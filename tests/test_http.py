import json
import sys
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import QuizServer, QuizHandler


class HTTPIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = QuizServer(('127.0.0.1', 0), QuizHandler)
        cls.host = 'http://127.0.0.1:%s' % cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, path, data=None, token=''):
        request = Request(self.host + path, data=json.dumps(data or {}, ensure_ascii=False).encode(), headers={
            'Content-Type': 'application/json', **({'X-Session-Token': token} if token else {})
        }, method='POST')
        with urlopen(request, timeout=4) as response:
            return json.load(response)

    def state(self, code, token):
        from urllib.parse import quote
        with urlopen(f'{self.host}/api/rooms/{code}/state?token={quote(token)}', timeout=4) as response:
            return json.load(response)

    def test_complete_two_student_lesson(self):
        room = self.post('/api/rooms')
        c, key = room['code'], room['token']
        s1 = self.post(f'/api/rooms/{c}/join', {'name':'가나다'})['token']
        s2 = self.post(f'/api/rooms/{c}/join', {'name':'라마바'})['token']
        lesson = {'question':'수업시간에 나온 숫자는?', 'options':['1','2','',''], 'correct_index':1}
        self.post(f'/api/rooms/{c}/draft', {**lesson, 'revision':2}, key)
        self.assertEqual(self.state(c, s1)['question'], lesson['question'])
        self.assertNotIn('correct_index', self.state(c, s1))
        self.post(f'/api/rooms/{c}/start', lesson, key)
        self.post(f'/api/rooms/{c}/answer', {'choice':1}, s1)
        self.post(f'/api/rooms/{c}/answer', {'choice':0}, s2)
        student = self.state(c, s1)
        self.assertEqual(student['you']['score'], 0)
        self.assertNotIn('correct_index', student)
        self.assertIsNone(student['you']['earned'])
        self.assertIsNone(student['you']['was_correct'])
        self.post(f'/api/rooms/{c}/close', {}, key)
        student = self.state(c, s1)
        self.assertEqual(student['correct_index'], 1)
        self.assertGreater(student['you']['score'], 1000)
        scores = [item['score'] for item in student['leaderboard']]
        self.assertEqual(len(scores), len(set(scores)))
        self.post(f'/api/rooms/{c}/next', {}, key)
        self.assertEqual(self.state(c, s1)['status'], 'draft')
        self.assertEqual(self.state(c, s1)['you']['score'], student['you']['score'])

    def test_permissions(self):
        room = self.post('/api/rooms')
        c, key = room['code'], room['token']
        token = self.post(f'/api/rooms/{c}/join', {'name':'손님'})['token']
        with self.assertRaises(HTTPError) as ctx:
            self.post(f'/api/rooms/{c}/start', {}, token)
        self.assertEqual(ctx.exception.code, 403)
        with self.assertRaises(HTTPError) as ctx:
            self.post(f'/api/rooms/{c}/answer', {'choice':1}, key)
        self.assertEqual(ctx.exception.code, 403)

    def test_live_stream_updates(self):
        room = self.post('/api/rooms')
        c, key = room['code'], room['token']
        token = self.post(f'/api/rooms/{c}/join', {'name':'테스트학생'})['token']
        stream = urlopen(f'{self.host}/api/rooms/{c}/events?token={token}', timeout=3)
        try:
            first = json.loads(stream.readline().decode('utf-8')[6:])
            self.assertEqual(first['status'], 'draft')
            self.post(f'/api/rooms/{c}/draft', {'question':'타이핑 즉시 도착', 'options':['보기1','보기2','',''], 'correct_index':0, 'revision':3}, key)
            stream.readline()  # blank separator from first message
            updated = json.loads(stream.readline().decode('utf-8')[6:])
            self.assertEqual(updated['question'], '타이핑 즉시 도착')
            self.assertNotIn('correct_index', updated)
        finally:
            stream.close()


if __name__ == '__main__':
    unittest.main()

class MultiplierHTTPTest(HTTPIntegrationTest):
    def test_multiplier_sent_through_live_stream_and_applied(self):
        room = self.post('/api/rooms')
        c, key = room['code'], room['token']
        player = self.post(f'/api/rooms/{c}/join', {'name':'반짝이'})['token']
        body = {'question': '물은 몇 도에서 얼까요?', 'options':['0도','10도','',''], 'correct_index':0, 'multiplier':'2.5'}
        stream = urlopen(f'{self.host}/api/rooms/{c}/events?token={player}', timeout=3)
        try:
            first = json.loads(stream.readline().decode('utf-8')[6:])
            self.assertEqual(first['multiplier'], 1)
            stream.readline()
            self.post(f'/api/rooms/{c}/draft', {**body, 'revision': 1}, key)
            updated = json.loads(stream.readline().decode('utf-8')[6:])
            self.assertEqual(updated['multiplier'], 2.5)
            self.assertNotIn('correct_index', updated)
        finally:
            stream.close()
        self.post(f'/api/rooms/{c}/start', body, key)
        self.post(f'/api/rooms/{c}/answer', {'choice':0}, player)
        self.assertIsNone(self.state(c, player)['you']['earned'])
        self.post(f'/api/rooms/{c}/close', {}, key)
        result = self.state(c, player)
        self.assertEqual(result['multiplier'], 2.5)
        self.assertGreaterEqual(result['you']['earned'], 2500)
        self.assertLessEqual(result['you']['earned'], 3750)
        self.post(f'/api/rooms/{c}/next', {}, key)
        self.assertEqual(self.state(c, player)['multiplier'], 1)
