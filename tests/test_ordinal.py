import unittest

from xuyi.ordinal import fact_key, facts, supersessions
from xuyi.retrieval import Retriever
from xuyi.store import Store

SCOPE = 'mab:test'


def chunk(event_id, text, at='2026-11-09T10:00:00+00:00'):
    return {'scope': SCOPE, 'event_id': event_id, 'sender_id': 'MAB', 'role': 'user',
            'origin': 'quoted', 'recorded_at': at, 'text': text}


class FactKeyTests(unittest.TestCase):
    def test_definite_description_keys_on_first_copula(self):
        self.assertEqual(fact_key('The author of Our Mutual Friend is Charles Dickens'),
                         ('the author of our mutual friend is', 'Charles Dickens'))

    def test_predicate_form_keys_on_first_preposition_after_predicate(self):
        # Lowercase particles inside the subject are not the predicate.
        self.assertEqual(fact_key('Albert of Saxony is affiliated with the religion of Islam'),
                         ('albert of saxony is affiliated with', 'the religion of Islam'))
        self.assertEqual(fact_key('Thomas Kyd was born in the city of London'),
                         ('thomas kyd was born in', 'the city of London'))
        # The object may itself contain prepositions; the key stops at the first one.
        self.assertEqual(fact_key('L. Ron Hubbard is a citizen of United States of America')[0],
                         'l. ron hubbard is a citizen of')

    def test_fallbacks_and_abstention(self):
        self.assertEqual(fact_key("Aristotle's child is Nicomachus"), ("aristotle's child is", 'Nicomachus'))
        # A title starting with "The" but without a copula falls back to the predicate form.
        self.assertEqual(fact_key('The Vampyre was written in the language of English')[0],
                         'the vampyre was written in')
        for unparseable in ('我喜欢喝茶', 'Hello', 'Paris', ''):
            self.assertIsNone(fact_key(unparseable))


class SupersessionTests(unittest.TestCase):
    def test_later_ordinal_wins_within_one_block(self):
        text = ('Here is a list of facts:\n'
                '107. The author of Our Mutual Friend is Charles Dickens.\n'
                '120. Chanel was founded by Coco Chanel.\n'
                '146. The author of Our Mutual Friend is Charles Darwin.')
        found = supersessions([chunk('c1', text)])
        self.assertEqual(len(found), 1)
        (old, new), = found.items()
        self.assertEqual(text[old[1]:].split('\n')[0], '107. The author of Our Mutual Friend is Charles Dickens.')
        self.assertEqual(new['ordinal'], 146)
        self.assertEqual(new['text'], '146. The author of Our Mutual Friend is Charles Darwin.')
        self.assertEqual(text[new['start']:new['end']], new['text'])

    def test_order_follows_time_then_ordinal_and_chains_point_to_newest(self):
        events = [chunk('c2', '1. Hines Ward plays the position of cornerback.', '2026-11-09T11:00:00+00:00'),
                  chunk('c1', '9. Hines Ward plays the position of wide receiver.\n'
                              '10. Hines Ward plays the position of quarterback.', '2026-11-09T10:00:00+00:00')]
        found = supersessions(events)
        self.assertEqual({key[0] for key in found}, {'c1'})
        self.assertEqual({new['text'] for new in found.values()},
                         {'1. Hines Ward plays the position of cornerback.'})

    def test_repeats_ties_and_plain_chat_are_left_alone(self):
        repeat = '1. Chanel was founded by Coco Chanel.\n2. Chanel was founded by coco  chanel.'
        self.assertEqual(supersessions([chunk('c1', repeat)]), {})
        tie = [chunk('a', '5. Chanel was founded by Coco Chanel.'),
               chunk('b', '5. Chanel was founded by Andy Warhol.')]
        self.assertEqual(supersessions(tie), {})  # 歧义即弃权
        chat = '1. 我喜欢喝茶\n2. 我喜欢喝咖啡\n我家的猫叫年糕。'
        self.assertEqual(facts([chunk('c1', chat)]), [])


class OrdinalRecallTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        filler = '\n'.join(f'{i}. Filler Person {i} was born in the city of Town{i}.' for i in range(2, 40))
        self.store.add_many([
            chunk('c1', '1. The author of Our Mutual Friend is Charles Dickens.\n' + filler),
            chunk('c2', '146. The author of Our Mutual Friend is Charles Darwin.', '2026-11-09T11:00:00+00:00')])

    def tearDown(self):
        self.store.close()

    def test_retrieved_stale_fact_carries_its_verbatim_successor(self):
        retriever = Retriever(self.store)
        plain = retriever.recall(SCOPE, 'Dickens Mutual Friend', 160)
        self.assertNotIn('Darwin', plain['context'])
        ruled = retriever.recall(SCOPE, 'Dickens Mutual Friend', 400, ordinal=True)
        self.assertIn('Dickens', ruled['context'])
        self.assertIn('｜后来更正为[c2] 146. The author of Our Mutual Friend is Charles Darwin.', ruled['context'])
        self.assertLessEqual(ruled['used'], 400)
        self.assertEqual(ruled['used'], len(ruled['context'].encode()))

    def test_switch_off_is_byte_identical_and_current_fact_is_not_annotated(self):
        retriever = Retriever(self.store)
        self.assertEqual(retriever.recall(SCOPE, 'Darwin', 400)['context'],
                         retriever.recall(SCOPE, 'Darwin', 400, ordinal=False)['context'])
        self.assertNotIn('后来更正为', retriever.recall(SCOPE, 'Darwin', 400, ordinal=True)['context'])

    def test_new_correction_takes_effect_on_the_same_retriever(self):
        retriever = Retriever(self.store)
        self.assertIn('[c2] 146.', retriever.recall(SCOPE, 'Dickens Mutual Friend', 400, ordinal=True)['context'])
        self.store.add(chunk('c3', '200. The author of Our Mutual Friend is Mary Shelley.', '2026-11-09T12:00:00+00:00'))
        context = retriever.recall(SCOPE, 'Dickens Mutual Friend', 400, ordinal=True)['context']
        self.assertIn('｜后来更正为[c3] 200. The author of Our Mutual Friend is Mary Shelley.', context)
        self.store.forget(SCOPE, 'c3')
        self.assertIn('[c2] 146.', retriever.recall(SCOPE, 'Dickens Mutual Friend', 400, ordinal=True)['context'])

    def test_annotation_is_skipped_when_successor_is_already_in_context(self):
        out = Retriever(self.store).recall(SCOPE, 'Darwin Dickens Mutual Friend', 2000, ordinal=True)
        self.assertEqual([p['event_id'] for p in out['sources']][:2], ['c2', 'c1'])
        self.assertIn('146. The author of Our Mutual Friend is Charles Darwin.', out['context'])
        self.assertEqual(out['context'].count('Charles Darwin'), 1)


if __name__ == '__main__':
    unittest.main()
