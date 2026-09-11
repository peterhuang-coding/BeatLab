"""BeatLab 反馈层：本地 Review 服务 + Rule Reweighting（PRD §7 Goal 5，P0 学习范围）。

serve（只绑 127.0.0.1）:
    .venv/bin/python pipeline/feedback.py serve [--port 8765]
    GET  /                      run/beat 列表
    GET  /review/<target>       动态生成 Review 页（audio 走 /media，反馈走 /api）
    GET  /media/<target>/...    试听文件（realpath 防目录穿越，仅 beats/ 内）
    GET  /api/ping              探活（页面 JS 判断「本地服务未启动」提示）
    GET  /api/feedback          查询反馈（?run_id=&candidate_id=）
    POST /api/feedback          收 JSON {run_id, candidate_id, dims, verdict, reasons} 落 feedback 表
    POST /api/keep|reject|regenerate|export  对应动作（keep 复制交付包 / export 写 Ableton 交付目录）
    POST /api/outcome           手动更新 ableton_outcome（'kept'/'exported' 占位后续人工更新）

reweight（Rule Reweighting，P0 学习范围）:
    .venv/bin/python pipeline/feedback.py --reweight
    读 feedback 表：被 reject 的候选其 hero 素材 source_id 累积负分（来源级降权），
    并按拒绝原因微调 recipe prior，写 ROOT/feedback_weights.json（供 Dev-2/3 经契约读取）。
    不承诺个性化收敛（P1 Personalized Reranker）。

契约（Dev-1 冻结）: common 提供 upsert_feedback()/get_feedback() 与 feedback 表
（id/run_id/candidate_id/dims_json/verdict/reasons_json/ableton_outcome/created_at）。
Dev-1 的 common 落地前，本文件用同契约 DDL 的本地 fallback 兜底（getattr 探测），
便于并行开发期独立验收；Dev-1 合并后自动切换到 common 实现。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import shutil
import sys
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import common
import report

# ---------- 契约 DDL（与 Dev-1 冻结契约一致，仅 fallback 用） ----------
FEEDBACK_DDL = """
CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    dims_json TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    ableton_outcome TEXT,
    created_at TEXT NOT NULL
);
"""
FEEDBACK_COLS = ("id", "run_id", "candidate_id", "dims_json", "verdict",
                 "reasons_json", "ableton_outcome", "created_at")

VALID_VERDICTS = {"keep", "reject", "regenerate", "export", "rated"}
# 拒绝原因 -> (recipe 类型, prior 调整值)：P0 recipe prior 微调规则
REASON_RECIPE_DELTA = {
    "chop_fragmented": ("chop", -0.10),   # 切得太碎 → chop prior 降
    "too_similar": ("loop", -0.10),       # 太像原曲 → loop prior 降
    "too_rigid": ("chop", -0.05),         # 太规整 → chop prior 微降
    "no_space": ("stem", -0.05),          # 没有空间 → stem prior 微降
    # sample_meh / drums_off 归因到素材来源与鼓组（composer），不调 recipe prior
    # worth_keeping 为正向原因，不参与降权
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row_id(run_id: str, candidate_id: str) -> str:
    return f"{run_id}:{candidate_id}"


# ---------- 契约访问层（common 优先，本地 fallback 兜底） ----------
def upsert_feedback(row: dict) -> str:
    """写入/更新一条反馈（契约: common.upsert_feedback 优先）。"""
    fn = getattr(common, "upsert_feedback", None)
    if fn is not None:
        conn = common.get_db()
        try:
            fn(conn, row)
        finally:
            conn.close()
        return row["id"]
    conn = common.get_db()
    try:
        conn.execute(FEEDBACK_DDL)
        conn.execute(
            "INSERT INTO feedback (id, run_id, candidate_id, dims_json, verdict, "
            "reasons_json, ableton_outcome, created_at) "
            "VALUES (:id, :run_id, :candidate_id, :dims_json, :verdict, :reasons_json, "
            ":ableton_outcome, :created_at) "
            "ON CONFLICT(id) DO UPDATE SET "
            "dims_json=excluded.dims_json, verdict=excluded.verdict, "
            "reasons_json=excluded.reasons_json, created_at=excluded.created_at, "
            "ableton_outcome=COALESCE(excluded.ableton_outcome, feedback.ableton_outcome)",
            row,
        )
        conn.commit()
    finally:
        conn.close()
    return row["id"]


def get_feedback(run_id: str | None = None, candidate_id: str | None = None) -> list[dict]:
    """查询反馈行（契约: common.get_feedback 优先），返回 dict 列表。"""
    fn = getattr(common, "get_feedback", None)
    if fn is not None:
        conn = common.get_db()
        try:
            rows = fn(conn, run_id=run_id)
        finally:
            conn.close()
        out = []
        for raw in rows:
            row = dict(raw)
            if candidate_id and row.get("candidate_id") != candidate_id:
                continue
            row["dims_json"] = json.dumps(
                row.pop("dims", {}) or {}, ensure_ascii=False, sort_keys=True)
            row["reasons_json"] = json.dumps(
                row.pop("reasons", []) or [], ensure_ascii=False)
            out.append(row)
        return out
    conn = common.get_db()
    try:
        conn.execute(FEEDBACK_DDL)
        q = "select id, run_id, candidate_id, dims_json, verdict, reasons_json, ableton_outcome, created_at from feedback where 1=1"
        args: list = []
        if run_id:
            q += " and run_id=?"
            args.append(run_id)
        if candidate_id:
            q += " and candidate_id=?"
            args.append(candidate_id)
        rows = conn.execute(q + " order by created_at", args).fetchall()
    finally:
        conn.close()
    return [dict(zip(FEEDBACK_COLS, r)) for r in rows]


# ---------- 反馈动作 ----------
def _save_feedback(run_id: str, candidate_id: str, dims: dict, verdict: str,
                   reasons: list[str], outcome: str | None = None) -> dict:
    row = {
        "id": _row_id(run_id, candidate_id),
        "run_id": run_id,
        "candidate_id": candidate_id,
        "dims_json": json.dumps(dims, ensure_ascii=False, sort_keys=True),
        "verdict": verdict,
        "reasons_json": json.dumps(reasons, ensure_ascii=False),
        "ableton_outcome": outcome,
        "created_at": _now(),
    }
    upsert_feedback(row)
    return row


def _norm_feedback(body: dict) -> tuple[str, str, dict, str, list[str]]:
    """校验/规整 POST 载荷，返回 (run_id, candidate_id, dims, verdict, reasons)。"""
    run_id = str(body.get("run_id") or "").strip()
    candidate_id = str(body.get("candidate_id") or "").strip()
    if not run_id or not candidate_id:
        raise ValueError("run_id/candidate_id 必填")
    dims_raw = body.get("dims") or {}
    if not isinstance(dims_raw, dict):
        raise ValueError("dims 需为对象")
    dims = {
        str(k): int(v) for k, v in dims_raw.items()
        if k in report.DIM_KEYS and isinstance(v, (int, float)) and 1 <= int(v) <= 5
    }
    verdict = str(body.get("verdict") or "rated")
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"verdict 需为 {sorted(VALID_VERDICTS)} 之一")
    reasons_raw = body.get("reasons") or []
    if not isinstance(reasons_raw, list):
        raise ValueError("reasons 需为数组")
    reasons = [str(r) for r in reasons_raw if r in report.REASON_CODES]
    return run_id, candidate_id, dims, verdict, reasons


def resolve_candidate(run_id: str, candidate_id: str):
    """候选目录 + manifests + spec（依赖 report 的结构识别）。"""
    cand_dir = report.candidate_dir(run_id, candidate_id)
    manifests = report.load_manifests(cand_dir)
    spec = report.load_spec(cand_dir)
    return cand_dir, manifests, spec


def action_keep(run_id: str, candidate_id: str) -> dict:
    """Keep: 标记保留 + 复制交付包到 ROOT/kept/<today>/<run>__<cand>/。"""
    cand_dir, _, _ = resolve_candidate(run_id, candidate_id)
    dst = common.ROOT / "kept" / common.today_str() / f"{run_id}__{candidate_id}"
    copied = report._copy_candidate_files(cand_dir, dst)
    _save_feedback(run_id, candidate_id, {}, "keep", [], outcome="kept")
    return {"ok": True, "message": f"已保留并复制交付包（{len(copied)} 项）", "kept_dir": str(dst)}


def action_reject(run_id: str, candidate_id: str, dims: dict, reasons: list[str]) -> dict:
    """Reject: 落 verdict=reject（--reweight 时计入来源降权）。"""
    resolve_candidate(run_id, candidate_id)
    _save_feedback(run_id, candidate_id, dims, "reject", reasons)
    return {"ok": True, "message": "已标记淘汰（--reweight 时计入来源级降权）"}


def action_regenerate(run_id: str, candidate_id: str) -> dict:
    """Regenerate: 落 verdict=regenerate + 写重生成请求 JSON 供生成层（Dev-3）经契约读取。"""
    resolve_candidate(run_id, candidate_id)
    jobs = common.ROOT / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    req = jobs / f"regenerate_{run_id}_{candidate_id}_{stamp}.json"
    req.write_text(json.dumps({
        "run_id": run_id, "candidate_id": candidate_id, "requested_at": _now(),
        "note": "Review 页 Regenerate 触发，供生成层经契约读取",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    _save_feedback(run_id, candidate_id, {}, "regenerate", [])
    return {"ok": True, "message": "已提交重生成请求", "request_file": str(req)}


def action_export(run_id: str, candidate_id: str) -> dict:
    """Export: 写 Ableton 交付目录 ROOT/exports/<run>/<cand>/
    （preview.wav/stems/midi/.als/recipe.json/provenance.json/run_manifest.json）。"""
    cand_dir, manifests, spec = resolve_candidate(run_id, candidate_id)
    dst = common.ROOT / "exports" / run_id / candidate_id
    copied = report._copy_candidate_files(cand_dir, dst)
    if (dst / "beat.wav").is_file() and not (dst / "preview.wav").exists():
        shutil.copy2(dst / "beat.wav", dst / "preview.wav")
        copied.append("preview.wav")
    # recipe.json：manifests.recipe 优先，legacy spec 兜底合成
    recipe = manifests.get("recipe") if isinstance(manifests.get("recipe"), dict) else None
    if recipe is None and spec is not None:
        from dataclasses import asdict
        recipe = {
            "type": "legacy", "bpm": spec.bpm, "style": spec.style,
            "sections": [asdict(s) for s in spec.sections],
            "total_bars": spec.total_bars or sum(s.bars for s in spec.sections),
            "sample_ids": list(spec.sample_ids),
        }
    if recipe is not None:
        (dst / "recipe.json").write_text(
            json.dumps(recipe, ensure_ascii=False, indent=2), encoding="utf-8")
        copied.append("recipe.json")
    # provenance.json：manifests.provenance 优先，否则合成（可追溯兜底）
    prov = manifests.get("provenance") if isinstance(manifests.get("provenance"), dict) else None
    if prov is None:
        prov = {"note": "legacy 兼容导出，无上游 provenance 记录", "source_dir": str(cand_dir)}
    prov = dict(prov)
    prov.setdefault("exported_at", _now())
    prov.setdefault("exported_from", str(cand_dir))
    (dst / "provenance.json").write_text(
        json.dumps(prov, ensure_ascii=False, indent=2), encoding="utf-8")
    copied.append("provenance.json")
    # run_manifest.json：本次交付文件清单
    (dst / "run_manifest.json").write_text(json.dumps({
        "run_id": run_id, "candidate_id": candidate_id, "exported_at": _now(),
        "files": copied, "export_dir": str(dst),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    copied.append("run_manifest.json")
    _save_feedback(run_id, candidate_id, {}, "export", [], outcome="exported")
    return {"ok": True, "message": f"已导出 Ableton 交付目录（{len(copied)} 项）", "export_dir": str(dst)}


def action_outcome(run_id: str, candidate_id: str, outcome: str) -> dict:
    """手动更新 ableton_outcome（'kept'/'exported' 占位后的人工结果，如 'opened'）。"""
    outcome = str(outcome or "").strip()
    if not outcome or len(outcome) > 40:
        raise ValueError("outcome 需为非空且 ≤40 字符")
    rows = get_feedback(run_id, candidate_id)
    base = rows[0] if rows else None
    row = {
        "id": _row_id(run_id, candidate_id),
        "run_id": run_id,
        "candidate_id": candidate_id,
        "dims_json": base["dims_json"] if base else "{}",
        "verdict": (base["verdict"] if base else "rated"),
        "reasons_json": base["reasons_json"] if base else "[]",
        "ableton_outcome": outcome,
        "created_at": base["created_at"] if base else _now(),
    }
    upsert_feedback(row)
    return {"ok": True, "message": f"已更新 ableton_outcome={outcome}"}


# ---------- Rule Reweighting ----------
def hero_source_of(cand_dir: Path, manifests: dict, spec) -> str | None:
    """候选 hero 素材的 source_id：manifests.hero.source_id → manifests.source_id
    → spec.sample_ids[0]（legacy 兜底，用 sample_id 充当来源键）。"""
    hero = None
    recipe = manifests.get("recipe")
    if isinstance(recipe, dict) and isinstance(recipe.get("hero"), dict):
        hero = recipe["hero"]
    elif isinstance(manifests.get("hero"), dict):
        hero = manifests["hero"]
    if hero:
        for k in ("source_id", "source", "asset_id"):
            if hero.get(k):
                return str(hero[k])
    for k in ("source_id", "hero_source_id"):
        if manifests.get(k):
            return str(manifests[k])
    if spec is not None and getattr(spec, "sample_ids", None):
        return str(spec.sample_ids[0])
    return None


def reweight(out_path: Path | None = None) -> tuple[dict, Path]:
    """读 feedback 表做来源级降权 + recipe prior 微调，写 feedback_weights.json。

    - source_penalty: 被 reject 候选的 hero 素材 source_id 累积 -1/次
    - recipe_prior: 按拒绝原因映射调整（REASON_RECIPE_DELTA）
    返回 (weights, 输出路径)。候选目录已删除的行跳过并提示。
    """
    rows = get_feedback()
    source_penalty: dict[str, int] = {}
    recipe_prior: dict[str, float] = {}
    skipped: list[str] = []
    reject_count = 0
    for r in rows:
        if r.get("verdict") != "reject":
            continue
        reject_count += 1
        run_id, cand_id = r["run_id"], r["candidate_id"]
        try:
            cand_dir, manifests, spec = resolve_candidate(run_id, cand_id)
        except FileNotFoundError:
            skipped.append(f"{run_id}/{cand_id}")
            continue
        src = hero_source_of(cand_dir, manifests, spec)
        if src:
            source_penalty[src] = source_penalty.get(src, 0) - 1
        try:
            reasons = json.loads(r.get("reasons_json") or "[]")
        except json.JSONDecodeError:
            reasons = []
        for reason in reasons:
            if reason in REASON_RECIPE_DELTA:
                rt, delta = REASON_RECIPE_DELTA[reason]
                recipe_prior[rt] = round(recipe_prior.get(rt, 0.0) + delta, 2)
    weights = {
        "generated_at": _now(),
        "source_penalty": source_penalty,
        "recipe_prior": recipe_prior,
        "reject_count": reject_count,
        "skipped_missing_candidates": skipped,
        "note": "P0 学习范围：反馈记录 + 来源级降权 + recipe prior 微调；"
                "供生成层（Dev-2/3）经契约读取，不承诺个性化收敛",
    }
    path = out_path or (common.ROOT / "feedback_weights.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(weights, ensure_ascii=False, indent=2), encoding="utf-8")
    return weights, path


# ---------- HTTP 服务（只绑 127.0.0.1） ----------
def list_targets() -> list[tuple[str, str]]:
    """ROOT/beats 下全部目标：(name, mode)。mode: run | legacy。"""
    beats = common.ROOT / "beats"
    if not beats.is_dir():
        return []
    out = []
    for d in sorted(beats.iterdir()):
        if not d.is_dir():
            continue
        if (d / "spec.json").is_file():
            out.append((d.name, "legacy"))
        elif any(p.is_dir() and (p / "spec.json").is_file() for p in d.iterdir()):
            out.append((d.name, "run"))
    return out


def _resolve_media_path(target: str, rel: str) -> Path | None:
    """/media 文件解析：仅允许 ROOT/beats/<target>/ 内的真实文件（防目录穿越）。"""
    base = (common.ROOT / "beats" / target).resolve()
    if not base.is_dir():
        return None
    cand = (base / rel).resolve()
    if cand == base or base not in cand.parents:
        return None
    return cand if cand.is_file() else None


class _Handler(BaseHTTPRequestHandler):
    server_version = "BeatLabFeedback/0.1"
    server_ref = None  # FeedbackServer 实例（启动时注入）

    def log_message(self, fmt, *args):  # 精简日志
        sys.stderr.write("[feedback] %s - %s\n" % (self.address_string(), fmt % args))

    # ---- 响应工具 ----
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _path(self) -> str:
        return urllib.parse.unquote(urllib.parse.urlparse(self.path).path)

    # ---- GET ----
    def do_GET(self):
        path = self._path()
        try:
            if path == "/":
                self._serve_index()
            elif path == "/api/ping":
                port = self.server_ref.port if self.server_ref else self.server.server_port
                self._send_json(200, {"ok": True, "host": f"127.0.0.1:{port}", "service": "beatlab-feedback"})
            elif path == "/api/feedback":
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                rows = get_feedback(qs.get("run_id", [None])[0], qs.get("candidate_id", [None])[0])
                out = []
                for r in rows:
                    item = dict(r)
                    try:
                        item["dims"] = json.loads(item.pop("dims_json") or "{}")
                    except json.JSONDecodeError:
                        item["dims"] = {}
                    try:
                        item["reasons"] = json.loads(item.pop("reasons_json") or "[]")
                    except json.JSONDecodeError:
                        item["reasons"] = []
                    out.append(item)
                self._send_json(200, {"ok": True, "rows": out})
            elif path.startswith("/review/"):
                self._serve_review(path[len("/review/"):])
            elif path.startswith("/media/"):
                self._serve_media(path[len("/media/"):])
            else:
                self._send_json(404, {"ok": False, "error": "not found"})
        except FileNotFoundError as exc:
            self._send_json(404, {"ok": False, "error": str(exc)})
        except Exception as exc:  # 兜底：服务不因单请求崩溃
            self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _serve_index(self):
        items = list_targets()
        cards = []
        for name, mode in items:
            label = "Run · A/B/C" if mode == "run" else "兼容 · 单候选"
            cards.append(
                f'<a class="card" href="/review/{urllib.parse.quote(name)}">'
                f'<div class="t">{name}</div><div class="m">{label}</div></a>'
            )
        page = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BeatLab · Review</title><style>{report.CSS}</style></head>
<body>
<header><h1>BeatLab · Review</h1>
<div class="sub">本地反馈服务 · 127.0.0.1 · {len(items)} 个目标 · {common.today_str()}</div></header>
<main><div class="grid">{''.join(cards) or '<div class="card">beats/ 下暂无可 Review 目标</div>'}</div></main>
<footer>BeatLab · feedback.py serve · P0 学习范围: 反馈记录 + 来源级降权 + recipe prior 微调</footer>
</body></html>
"""
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_review(self, target: str):
        target = target.strip("/")
        if not target:
            self._send_json(400, {"ok": False, "error": "缺少 target"})
            return
        mode, candidates = report.detect_target(target)
        port = self.server_ref.port if self.server_ref else self.server.server_port
        page = report.build_review_page(
            target, mode, candidates,
            media_base=lambda c: f"/media/{target}/{c}/" if mode == "run" else f"/media/{target}/",
            cfg_extra={"host": f"127.0.0.1:{port}"},
        )
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_media(self, rest: str):
        target, _, rel = rest.strip("/").partition("/")
        f = _resolve_media_path(target, rel)
        if f is None:
            self._send_json(404, {"ok": False, "error": "media 文件不存在"})
            return
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if f.suffix == ".als":
            ctype = "application/x-ableton-live-set"
        self._send(200, f.read_bytes(), ctype)

    # ---- POST ----
    def do_POST(self):
        path = self._path()
        try:
            body = self._read_body()
            if path == "/api/feedback":
                run_id, cand_id, dims, verdict, reasons = _norm_feedback(body)
                resolve_candidate(run_id, cand_id)
                _save_feedback(run_id, cand_id, dims, verdict, reasons)
                self._send_json(200, {"ok": True, "message": "反馈已记录", "verdict": verdict})
            elif path == "/api/keep":
                run_id, cand_id, _, _, _ = _norm_feedback(body)
                self._send_json(200, action_keep(run_id, cand_id))
            elif path == "/api/reject":
                run_id, cand_id, dims, _, reasons = _norm_feedback(body)
                self._send_json(200, action_reject(run_id, cand_id, dims, reasons))
            elif path == "/api/regenerate":
                run_id, cand_id, _, _, _ = _norm_feedback(body)
                self._send_json(200, action_regenerate(run_id, cand_id))
            elif path == "/api/export":
                run_id, cand_id, _, _, _ = _norm_feedback(body)
                self._send_json(200, action_export(run_id, cand_id))
            elif path == "/api/outcome":
                run_id = str(body.get("run_id") or "").strip()
                cand_id = str(body.get("candidate_id") or "").strip()
                if not run_id or not cand_id:
                    raise ValueError("run_id/candidate_id 必填")
                self._send_json(200, action_outcome(run_id, cand_id, body.get("outcome", "")))
            else:
                self._send_json(404, {"ok": False, "error": "not found"})
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
        except FileNotFoundError as exc:
            self._send_json(404, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("请求体为空")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("请求体需为 UTF-8 JSON")
        if not isinstance(data, dict):
            raise ValueError("JSON 需为对象")
        return data


class FeedbackServer:
    """本地反馈服务：只绑定 127.0.0.1（禁止其他地址）。"""

    def __init__(self, port: int = 8765):
        self.host = "127.0.0.1"
        _Handler.server_ref = self
        self.httpd = ThreadingHTTPServer((self.host, port), _Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]

    def serve_forever(self):
        print(f"[feedback] Review 服务: http://{self.host}:{self.port}/ （仅本机）", file=sys.stderr)
        print("[feedback] 停止后 Review 页自动提示「本地服务未启动」，静态浏览不受影响", file=sys.stderr)
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[feedback] 已停止", file=sys.stderr)
        finally:
            self.shutdown()

    def shutdown(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="BeatLab 反馈层：本地 Review 服务（只绑 127.0.0.1）+ Rule Reweighting")
    ap.add_argument("--reweight", action="store_true",
                    help="读 feedback 表做来源级降权与 recipe prior 微调，写 ROOT/feedback_weights.json")
    ap.add_argument("cmd", nargs="?", default="serve", choices=("serve",),
                    help="serve: 启动本地服务")
    ap.add_argument("--port", type=int, default=8765, help="服务端口（默认 8765，仍只绑 127.0.0.1）")
    args = ap.parse_args()
    if args.reweight:
        weights, path = reweight()
        print(f"[feedback] reject {weights['reject_count']} 行 → "
              f"source_penalty={weights['source_penalty']} recipe_prior={weights['recipe_prior']}")
        if weights.get("skipped_missing_candidates"):
            print(f"[feedback] 跳过已删除候选: {weights['skipped_missing_candidates']}", file=sys.stderr)
        print(f"[feedback] 已写 {path}")
        return
    if args.cmd == "serve":
        FeedbackServer(port=args.port).serve_forever()


if __name__ == "__main__":
    main()
