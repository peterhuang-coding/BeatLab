"""Render a traceable region of a musical phrase, preserving its target timing."""
from pathlib import Path
import shutil
import subprocess
import tempfile

import numpy as np
import soundfile as sf


def make_slice(source: Path, *, start_s: float, end_s: float,
               duration_s: float, semitones: float = 0,
               reverse: bool = False) -> tuple[np.ndarray, int]:
    info=sf.info(source)
    if not all(np.isfinite(v) for v in (start_s,end_s,duration_s,semitones)):
        raise ValueError('Non-finite slice parameter')
    start,end=round(start_s*info.samplerate),round(end_s*info.samplerate)
    target=round(duration_s*info.samplerate)
    if not (0<=start<end<=info.frames and target>0 and abs(semitones)<=24):
        raise ValueError('Slice outside source or invalid duration/pitch')
    audio,sr=sf.read(source,start=start,stop=end,always_2d=True,dtype='float32')
    if audio.shape[1]>2 or not np.isfinite(audio).all():
        raise ValueError('Expected finite mono or stereo source')
    if audio.shape[1]==1:
        audio=np.repeat(audio,2,axis=1)
    if reverse:
        audio=audio[::-1].copy()
    if semitones or len(audio)!=target:
        executable=shutil.which('rubberband')
        if executable is None:
            raise RuntimeError('Rubber Band CLI is required for independent time/pitch changes')
        with tempfile.TemporaryDirectory(prefix='beatlab-phrase-') as temp:
            raw=Path(temp)/'in.wav';out=Path(temp)/'out.wav'
            sf.write(raw,audio,sr,subtype='FLOAT')
            subprocess.run([executable,'-q','-3','--centre-focus','-D',str(duration_s),
                            '-p',str(semitones),str(raw),str(out)],check=True,
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90)
            audio,new_sr=sf.read(out,always_2d=True,dtype='float32')
            if new_sr!=sr or abs(len(audio)-target)>max(4,round(sr*.05)):
                raise ValueError('Time-stretch engine did not meet requested duration')
        if len(audio)<target:
            audio=np.pad(audio,((0,target-len(audio)),(0,0)))
        audio=audio[:target]
    # Short edge fades protect the cut; musical gates stay in the arrangement.
    fade=min(round(.004*sr),len(audio)//2)
    if fade:
        audio[:fade]*=np.linspace(0,1,fade)[:,None]
        audio[-fade:]*=np.linspace(1,0,fade)[:,None]
    if not np.isfinite(audio).all():
        raise ValueError('Pitch/stretch returned non-finite audio')
    return audio,sr
