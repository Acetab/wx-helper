"""验证整个词池的可接性，不调用网络或模型。"""
import random
import unittest

from plugins.idiom_chain.lexicon import Lexicon, WORDS
from plugins.idiom_chain.judge import IdiomJudge
from wxbot_framework.text import FOUR_CHINESE


class LexiconTests(unittest.TestCase):
    def test_words_are_unique_four_character_entries(self):
        self.assertEqual(len(WORDS), len(set(WORDS)))
        self.assertTrue(all(FOUR_CHINESE.fullmatch(word) for word in WORDS))

    def test_all_bot_candidates_leave_three_distinct_unused_answers(self):
        lexicon = Lexicon()
        # 覆盖全部出题候选，而非对大词库逐词重复遍历同一个首字的候选。
        for word in lexicon.playable(lexicon.words, ()):
            with self.subTest(word=word):
                replies = lexicon.continuations(word, {word})
                common = set(replies) & lexicon.common_words
                self.assertGreaterEqual(len(common), 3)
                self.assertEqual(len(replies), len(set(replies)))
                self.assertNotIn(word, replies)

    def test_threshold_degrades_before_a_dead_end_is_declared(self):
        lexicon = Lexicon(("意气风发", "发扬光大", "发号施令", "大快人心", "大公无私", "大功告成"))
        # 还有 2 个接法：放宽门槛继续出题，而不是直接认输。
        self.assertEqual(lexicon.choose("意气风发", ("意气风发",)), "发扬光大")
        self.assertEqual(lexicon.choose("意气风发", ("意气风发", "大快人心")), "发扬光大")
        # 唯一候选没有后续，才算真正的死路。
        self.assertEqual(lexicon.choose("意气风发", ("意气风发", "发扬光大")), "")

    def test_seeded_games_never_repeat_or_leave_player_without_choices(self):
        for seed in range(100):
            lexicon = Lexicon(rng=random.Random(seed))
            current = lexicon.opening()
            used = {current}
            for _ in range(10):
                answers = lexicon.continuations(current, used)
                # 门槛会逐级放宽到 1，但绝不把玩家留在无解的死路上。
                self.assertGreaterEqual(len(answers), 1)
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

    def test_dictionary_has_full_offline_coverage(self):
        lexicon = Lexicon()
        self.assertGreaterEqual(len(lexicon.words), 29000)
        self.assertTrue(set(WORDS) <= lexicon.words)
        self.assertTrue(all(FOUR_CHINESE.fullmatch(word) for word in lexicon.words))
        self.assertIn("意料之外", lexicon.words)
        self.assertNotIn("意甲乙丙", lexicon.words)

    def test_bot_can_use_expansion_when_small_pool_cannot_continue(self):
        lexicon = Lexicon(rng=random.Random(42))
        self.assertEqual(Lexicon(WORDS).choose("好高骛远", {"好高骛远"}), "")
        following = lexicon.choose("好高骛远", {"好高骛远"})
        self.assertTrue(following)
        self.assertEqual(following[0], "远")
        common = set(lexicon.continuations(following, {"好高骛远", following})) & lexicon.common_words
        self.assertGreaterEqual(len(common), 3)
        self.assertTrue(IdiomJudge(None, lexicon=lexicon).evaluate("心想事成", "成千上万", ()).accepted)

    def test_many_obscure_replies_do_not_replace_common_reply_requirement(self):
        words = ("意气风发", "发扬光大", "大甲乙丙", "大乙丙丁", "大丙丁戊")
        lexicon = Lexicon(words, common_words={"意气风发", "发扬光大"})
        self.assertEqual(len(lexicon.continuations("发扬光大", ())), 3)
        self.assertEqual(lexicon.choose("意气风发", ()), "")

    def test_common_suggestions_come_first_and_used_words_are_excluded(self):
        lexicon = Lexicon()
        choices = lexicon.suggestions("意气风发", {"发扬光大"})
        self.assertIn(choices[0], lexicon.common_words)
        self.assertNotIn("发扬光大", choices)
        first_expanded = next(i for i, word in enumerate(choices) if word not in lexicon.common_words)
        self.assertTrue(all(word in lexicon.common_words for word in choices[:first_expanded]))

    def test_openings_include_expansion_and_always_have_common_answers(self):
        lexicon = Lexicon(rng=random.Random(42))
        openings = [lexicon.opening() for _ in range(40)]
        self.assertTrue(any(word not in WORDS for word in openings))
        self.assertTrue(any(word in WORDS for word in openings))
        for word in openings:
            common = set(lexicon.continuations(word, {word})) & lexicon.common_words
            self.assertGreaterEqual(len(common), 3)
