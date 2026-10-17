"""验证整个词池的可接性，不调用网络或模型。"""
import random
import unittest

from plugins.idiom_chain.lexicon import Lexicon, WORDS
from wxbot_framework.text import FOUR_CHINESE


class LexiconTests(unittest.TestCase):
    def test_words_are_unique_four_character_entries(self):
        self.assertEqual(len(WORDS), len(set(WORDS)))
        self.assertTrue(all(FOUR_CHINESE.fullmatch(word) for word in WORDS))

    def test_all_bot_candidates_leave_three_distinct_unused_answers(self):
        lexicon = Lexicon()
        for previous in sorted(lexicon.words):
            used = {previous}
            for word in lexicon.playable(lexicon.continuations(previous, used), used):
                with self.subTest(previous=previous, word=word):
                    self.assertEqual(word[0], previous[-1])
                    replies = lexicon.continuations(word, used | {word})
                    self.assertGreaterEqual(len(replies), 3)
                    self.assertEqual(len(replies), len(set(replies)))
                    self.assertFalse(set(replies) & (used | {word}))

    def test_exhausted_replies_are_not_counted_and_dead_ends_are_excluded(self):
        lexicon = Lexicon(("意气风发", "发扬光大", "发号施令", "大快人心", "大公无私", "大功告成"))
        self.assertEqual(lexicon.choose("意气风发", ("意气风发",)), "发扬光大")
        self.assertEqual(lexicon.choose("意气风发", ("意气风发", "大快人心")), "")
        self.assertEqual(lexicon.choose("意气风发", ("意气风发", "发扬光大")), "")

    def test_seeded_games_never_repeat_or_leave_player_without_choices(self):
        for seed in range(100):
            lexicon = Lexicon(rng=random.Random(seed))
            current = lexicon.opening()
            used = {current}
            for _ in range(10):
                answers = lexicon.continuations(current, used)
                self.assertGreaterEqual(len(answers), 3)
                player = lexicon.rng.choice(answers)
                self.assertNotIn(player, used)
                used.add(player)
                following = lexicon.choose(player, used)
                if not following:
                    break  # 已明确定义为全群提前获胜，不给玩家出死题。
                self.assertNotIn(following, used)
                self.assertEqual(following[0], player[-1])
                used.add(following)
                current = following

    def test_opening_is_not_fixed(self):
        lexicon = Lexicon(rng=random.Random(42))
        self.assertGreater(len({lexicon.opening() for _ in range(30)}), 5)
