from datetime import timedelta
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from quadringent.contract import JournalPosition
from quadringent.proof_windows import begin_window
import test_proof_windows

NOW = test_proof_windows.NOW


class WindowEligibilityTests(unittest.TestCase):
    def test_invalid_intent_is_rejected_before_first_scan(self):
        from quadringent.proof_windows import close_eligible_window
        with TemporaryDirectory() as directory:
            _,store,checkpoint,_=test_proof_windows.ProofWindowTests().setup_store(directory)
            intent=begin_window(store,checkpoint,window_id='w1',started_at=NOW,duration_seconds=600)
            for field,value in (('format_version','invalid'),('window_id','other'),('extra',True)):
                with self.subTest(field=field):
                    invalid=dict(intent,**{field:value})
                    original=store.get_bounded
                    with patch.object(store,'get_bounded',side_effect=lambda key,limit:json.dumps(invalid).encode() if key.endswith('/intent.json') else original(key,limit)):
                        with self.assertRaises(ValueError):close_eligible_window(store,checkpoint,window_id='w1',now=NOW)

    def test_before_deadline_then_first_eligible_scan_closes(self):
        from quadringent.proof_windows import close_eligible_window, read_closed_window
        with TemporaryDirectory() as directory:
            _,store,checkpoint,writer=test_proof_windows.ProofWindowTests().setup_store(directory)
            begin_window(store,checkpoint,window_id='w1',started_at=NOW,duration_seconds=600)
            writer.capture_receipted_window(start=JournalPosition('R1',10),end=JournalPosition('R1',20),previous=JournalPosition('R1',9),scan_completed_at=NOW+timedelta(seconds=599))
            self.assertIsNone(close_eligible_window(store,checkpoint,window_id='w1',now=NOW+timedelta(seconds=599)))
            writer.capture_receipted_window(start=JournalPosition('R1',21),end=JournalPosition('R1',30),previous=JournalPosition('R1',20),scan_completed_at=NOW+timedelta(seconds=600))
            closed=close_eligible_window(store,checkpoint,window_id='w1',now=NOW+timedelta(seconds=602))
            self.assertEqual(closed['closed_at'],(NOW+timedelta(seconds=600)).isoformat())
            self.assertEqual(closed['sealed_at'],(NOW+timedelta(seconds=602)).isoformat())
            self.assertEqual(closed,read_closed_window(store,window_id='w1'))
            with self.assertRaises(ValueError):close_eligible_window(store,checkpoint,window_id='w1',now=NOW)
            self.assertEqual(close_eligible_window(store,checkpoint,window_id='w1',now=NOW+timedelta(days=2)),closed)

    def test_future_regression_extra_scan_and_late_seal_are_rejected(self):
        from quadringent.proof_windows import close_eligible_window
        for stamps, observed in (((601,),600), ((590,580,600),601), ((600,601),602), ((600,),661)):
            with self.subTest(stamps=stamps,observed=observed),TemporaryDirectory() as directory:
                root,store,checkpoint,writer=test_proof_windows.ProofWindowTests().setup_store(directory)
                begin_window(store,checkpoint,window_id='w1',started_at=NOW,duration_seconds=600)
                previous=9
                for stamp in stamps:
                    end=previous+10
                    writer.capture_receipted_window(start=JournalPosition('R1',previous+1),end=JournalPosition('R1',end),previous=JournalPosition('R1',previous),scan_completed_at=NOW+timedelta(seconds=stamp))
                    previous=end
                with self.assertRaises(ValueError):close_eligible_window(store,checkpoint,window_id='w1',now=NOW+timedelta(seconds=observed))
                self.assertFalse((root/'objects'/'windows'/'w1'/'closed.json').exists())
