import unittest
from pathlib import Path

from dispatch import DispatchError
from review_watch import temporary_review


class FakeRPC:
    def __init__(self, items):
        self.items = iter(items)
        self.calls = []

    def call(self, method, params, *, on_notification=None):
        self.calls.append((method, params))
        if method == 'thread/start':
            return {'thread': {'id': 'temporary', 'ephemeral': True, 'path': None},
                    'model': 'gpt-6.2-sol', 'approvalPolicy': 'never', 'sandbox': {'type': 'readOnly'}}
        if method == 'turn/start':
            on_notification(self.receive(None))  # An item can be delivered before start's reply.
            return {'turn': {'id': 'turn'}}
        if method == 'thread/unsubscribe':
            return {'status': 'unsubscribed'}
        raise AssertionError(method)

    def receive(self, deadline):
        return next(self.items)

    def close(self):
        self.calls.append(('close', {}))


def item(thread, kind, **fields):
    return {'method': 'item/completed', 'params': {
        'threadId': thread, 'turnId': 'turn', 'item': {'type': kind, **fields}}}


def terminal(status):
    return {'method': 'turn/completed', 'params': {'threadId': 'temporary',
                                                  'turn': {'id': 'turn', 'status': status}}}


class TemporaryReviewTests(unittest.TestCase):
    def review(self, fake):
        return temporary_review({'thread_id': 'coordinator'}, {
            'model': 'gpt-6.2-sol', 'effort': 'medium', 'prompt': 'frozen input',
        }, Path('/tmp'), 1, client_factory=lambda config: fake)

    def test_fresh_context_and_attributable_answer(self):
        fake = FakeRPC([
            item('unrelated', 'agentMessage', phase='final_answer', text='other context'),
            item('temporary', 'agentMessage', phase='commentary', text='preamble'),
            item('temporary', 'agentMessage', phase='final_answer', text='final'),
            terminal('completed'),
        ])
        self.assertEqual(self.review(fake), 'final')
        self.assertEqual([method for method, _ in fake.calls],
                         ['thread/start', 'turn/start', 'thread/unsubscribe', 'close'])
        self.assertTrue(fake.calls[0][1]['ephemeral'])
        self.assertEqual(fake.calls[1][1]['threadId'], 'temporary')

    def test_completed_tool_is_rejected_and_context_released(self):
        fake = FakeRPC([item('temporary', 'commandExecution')])
        with self.assertRaisesRegex(DispatchError, 'out-of-scope'):
            self.review(fake)
        self.assertEqual(fake.calls[-2], ('thread/unsubscribe', {'threadId': 'temporary'}))

    def test_terminal_failure_does_not_accept_an_answer(self):
        fake = FakeRPC([item('temporary', 'agentMessage', phase='final_answer', text='final'), terminal('failed')])
        with self.assertRaisesRegex(DispatchError, 'confirmed'):
            self.review(fake)
