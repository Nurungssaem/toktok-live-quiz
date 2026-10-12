import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import APIError, Room


class RoomTest(unittest.TestCase):
    def setUp(self):
        self.room = Room(code='ABC234', teacher_token='TEACHER_SECRET')

    def prepared_room(self):
        with self.room.lock:
            t1, _ = self.room.join('하늘')
            t2, _ = self.room.join('민준')
            self.room.set_draft({'question':'1 + 1은?', 'options':['1','2','3','4'], 'correct_index':1, 'revision':1})
            return t1, t2

    def test_correct_index_not_sent_to_student_until_end(self):
        a, b = self.prepared_room()
        student_draft = self.room.snapshot(a)
        self.assertNotIn('correct_index', student_draft)
        self.assertEqual(student_draft['options'], ['1', '2', '3', '4'])
        self.assertIn('correct_index', self.room.snapshot('TEACHER_SECRET'))
        self.room.start({'question':'1 + 1은?', 'options':['1','2','3','4'], 'correct_index':1})
        student_live = self.room.snapshot(a)
        self.assertNotIn('correct_index', student_live)
        self.assertNotIn('was_correct', student_live)
        self.room.submit_answer(self.room.players[a], {'choice':1})
        self.assertNotIn('correct_index', self.room.snapshot(b))
        self.assertEqual(self.room.snapshot(a)['you']['score'], 0) # Not even score should reveal correctness during play.
        self.room.close()
        self.assertEqual(self.room.snapshot(a)['correct_index'], 1)

    def test_unique_scores_across_many_students(self):
        tokens = []
        for i in range(40):
            token, _ = self.room.join('학생'+str(i))
            tokens.append(token)
        self.room.start({'question':'정답?', 'options':['맞음','틀림','',''], 'correct_index':0})
        for i, token in enumerate(tokens):
            if i % 4 == 0:
                self.room.submit_answer(self.room.players[token], {'choice':0})
            elif i % 4 == 1:
                self.room.submit_answer(self.room.players[token], {'choice':1})
        self.room.close()
        scores = [p.score for p in self.room.players.values()]
        self.assertEqual(len(scores), len(set(scores)))
        self.assertTrue(all(isinstance(score, int) for score in scores))
        self.assertEqual(len(self.room.snapshot(tokens[0])['leaderboard']), 40)
        self.assertEqual(self.room.snapshot(tokens[0])['leaderboard'][0]['rank'], 1)

    def test_malformed_and_duplicate_rejection(self):
        a, b = self.prepared_room()
        with self.assertRaises(APIError): self.room.join('하늘')
        with self.assertRaises(APIError): self.room.submit_answer(self.room.players[a], {'choice':1})
        with self.assertRaises(APIError): self.room.start({'question':'', 'options':['','','',''], 'correct_index':0})
        self.room.start({'question':'1 + 1은?', 'options':['1','2','3','4'], 'correct_index':1})
        with self.assertRaises(APIError): self.room.set_draft({'question':'changed', 'options':['a','b','c','d'], 'correct_index':0, 'revision':2})
        self.room.submit_answer(self.room.players[a], {'choice':1})
        with self.assertRaises(APIError): self.room.submit_answer(self.room.players[a], {'choice':0})
        self.room.close()
        self.room.next()
        self.assertEqual(self.room.players[a].score > 0, True)
        self.assertIsNone(self.room.players[a].answer)

    def test_late_draft_cannot_overwrite_latest(self):
        self.room.set_draft({'question':'새 질문', 'options':['가','나','다','라'], 'correct_index':2, 'revision':15})
        self.room.set_draft({'question':'옛 질문', 'options':['x','y','z','w'], 'correct_index':1, 'revision':14})
        self.assertEqual(self.room.question, '새 질문')
        self.assertEqual(self.room.correct_index, 2)

    def test_auth_and_names(self):
        token, _ = self.room.join('<img src=x>')
        self.assertEqual(self.room.authenticate(token)[0], 'student')
        with self.assertRaises(APIError): self.room.authenticate('invalid')
        with self.assertRaises(APIError): self.room.must_be_teacher(token)
        with self.assertRaises(APIError): self.room.must_be_student('TEACHER_SECRET')


if __name__ == '__main__':
    unittest.main()

class MultiplierTest(unittest.TestCase):
    def setUp(self):
        self.room = Room(code='ZXCV23', teacher_token='TEACHER')
        self.a, _ = self.room.join('수아')
        self.b, _ = self.room.join('현우')
        self.base = {'question': '3 곱하기 3은?', 'options': ['6', '9', '12', '15'], 'correct_index': 1}

    def test_multiplier_visible_but_answer_key_is_hidden(self):
        self.room.set_draft({**self.base, 'multiplier': '2.25', 'revision': 1})
        student = self.room.snapshot(self.a)
        self.assertEqual(student['multiplier'], 2.25)
        self.assertNotIn('correct_index', student)
        self.assertEqual(self.room.snapshot('TEACHER')['multiplier'], 2.25)
        self.room.start({**self.base, 'multiplier': '2.25'})
        student = self.room.snapshot(self.a)
        self.assertEqual(student['multiplier'], 2.25)
        self.assertNotIn('correct_index', student)
        self.assertEqual(student['you']['score'], 0)
        self.assertIsNone(student['you']['earned'])

    def test_decimal_scoring_and_reset_every_round(self):
        self.room.start({**self.base, 'multiplier': 1.5})
        player = self.room.players[self.a]
        self.room.submit_answer(player, {'choice': 1})
        raw_points = 1000 + max(0, 500 - player.elapsed_ms // 60)
        self.assertEqual(player.earned, (raw_points * 150 + 50) // 100)
        self.room.submit_answer(self.room.players[self.b], {'choice': 0})
        self.assertEqual(self.room.players[self.b].earned, 0)
        self.room.close()
        prior_score = player.score
        self.room.next()
        self.assertEqual(self.room.snapshot(self.a)['multiplier'], 1)
        self.room.start(self.base)
        self.room.submit_answer(player, {'choice': 1})
        raw_points2 = 1000 + max(0, 500 - player.elapsed_ms // 60)
        self.assertEqual(player.earned, raw_points2)
        self.room.close()
        self.assertGreater(player.score, prior_score)

    def test_valid_small_large_and_fractional_multipliers(self):
        for revision, value in enumerate(['0.01', '0.1', '1', '1.5', '2.25', '3', '99.99', '100'], 1):
            self.room.set_draft({**self.base, 'multiplier': value, 'revision': revision})
            self.assertAlmostEqual(self.room.snapshot(self.a)['multiplier'], float(value))

    def test_invalid_multiplier_rejected_and_unmodified(self):
        self.room.set_draft({**self.base, 'multiplier': '2', 'revision': 1})
        for value in [0, -1, '100.01', '1.234', '', None, True, [], {}, 'NaN', 'Infinity', 'not a number']:
            with self.subTest(value=value), self.assertRaises(APIError) as e:
                self.room.set_draft({**self.base, 'multiplier': value, 'revision': 2})
            self.assertEqual(e.exception.status, 400)
            self.assertEqual(self.room.snapshot(self.a)['multiplier'], 2)
        with self.assertRaises(APIError):
            self.room.start({**self.base, 'multiplier': '0'})
        self.assertEqual(self.room.status, 'draft')

    def test_earlier_revision_does_not_reset_multiplier(self):
        self.room.set_draft({**self.base, 'multiplier': '3.5', 'revision': 100})
        self.room.set_draft({**self.base, 'multiplier': '1', 'revision': 99})
        self.assertEqual(self.room.snapshot(self.a)['multiplier'], 3.5)
