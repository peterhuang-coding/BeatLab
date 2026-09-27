import unittest
from examples.prepare_city_afterimages import select_windows


class CityPreparationTests(unittest.TestCase):
    def test_short_or_unsorted_grid_rejected(self):
        for beats in ([0,1,2], [0,1,2,3,4,5,6,7,8,8,10]):
            with self.assertRaises(ValueError):
                select_windows({'beats':beats,'duration':12},90,0)

    def test_half_double_time_sources_stay_bounded_and_nonoverlapping(self):
        for period in (.30,.5,.75):
            for bpm in (84,92,112,140):
                cuts,turn=select_windows({'beats':[i*period for i in range(40)],'duration':40*period},bpm,3)
                self.assertEqual(len(set(cuts)),4)
                for index,(a,b) in enumerate(cuts):
                    self.assertGreater(b,a)
                    if index:self.assertEqual(a,cuts[index-1][1])
                    self.assertTrue(.55<=120/bpm/(b-a)<=1.65)
                self.assertTrue(.55<=240/bpm/(turn[1]-turn[0])<=1.65)

    def test_extreme_tempo_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            select_windows({'beats':[i*.01 for i in range(40)],'duration':1},84,0)

    def test_out_of_source_grid_rejected(self):
        with self.assertRaises(ValueError):
            select_windows({'beats':[i*.5 for i in range(40)],'duration':3},100,0)


if __name__=='__main__':unittest.main()
