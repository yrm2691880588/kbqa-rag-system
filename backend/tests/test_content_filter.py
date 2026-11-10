"""内容安全审核（本地敏感词库）测试 —— P1-F。

这个文件在测什么（大白话）：
    系统要用大模型生成回答，万一生成出违规内容，责任在平台。
    所以上线前要有一道闸把这类内容拦住。本文件验证这道闸是不是真的管用。

    最值得较真的不是"能不能拦住正常写法的敏感词"（那太容易了），
    而是**能不能拦住故意绕过的写法**——因为真正要发违规内容的人，
    不会老实写"赌博"，他会写"赌  博"、"赌-博"、"ＧＡＭＢＬＥ"。
    如果只按原样比对词表，这些全都躲得过去，功能等于摆设。

    所以这个文件里一半的用例是"花式绕过"验证。

运行方式（backend 目录下）：
    python -m unittest tests.test_content_filter -v
"""

import os
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import content_filter as cf


@contextmanager
def temp_wordlist(content: str):
    """临时把"词表文件"换成本用例自己写的内容。

    为什么要走真实文件、而不是直接往内存缓存里塞：
        第一版测试就是直接改 cf._cache，结果**测试通过了假的**——
        因为 check() 内部会调 load_wordlist()，发现"缓存里的词数/时间戳
        跟磁盘对不上"，于是又从磁盘重新读了一遍，把我的注入整个覆盖掉。
        也就是说：注入根本没生效，断言却全绿。改成写临时文件 + 改路径，
        走的才是和线上完全一样的加载链路。
    """
    fd, name = tempfile.mkstemp(suffix=".txt", prefix="kbqa_wordlist_")
    os.close(fd)
    path = Path(name)
    path.write_text(content, encoding="utf-8")
    try:
        with unittest.mock.patch.object(cf, "_wordlist_path", return_value=path):
            cf.load_wordlist(force=True)
            yield
    finally:
        # 还原成项目自带的词表，避免污染后续用例
        with unittest.mock.patch.object(cf, "_wordlist_path", return_value=cf._WORDLIST_PATH):
            cf.load_wordlist(force=True)
        path.unlink(missing_ok=True)


class WordListTestCase(unittest.TestCase):
    """词表本身能不能正确读进来。"""

    def test_wordlist_loads_from_disk(self):
        """内置词表必须能读到，而且要有内容。

        为什么加这条：如果文件路径写错、或者被打包时漏掉，
        check() 会"静默地谁都不拦"——功能看着没报错，实际完全失效。
        这类"静默失效"最难发现，所以要有断言盯着词数。
        """
        wl = cf.load_wordlist(force=True)
        self.assertGreater(len(wl.words), 20, "敏感词表几乎是空的，审核功能实际处于失效状态")
        self.assertEqual(len(wl.words), len(wl.categories), "词与分类数量对不上")

    def test_comment_lines_are_not_words(self):
        """以 # 开头的行是分类标题，不能变成敏感词。"""
        wl = cf._parse_wordlist("# 分类：测试\n加微信\n\n# 另一类\n赌博\n")
        self.assertIn("加微信", wl.words)
        self.assertIn("赌博", wl.words)
        for w in wl.words:
            self.assertFalse(w.startswith("#"), f"注释行被当成了敏感词：{w}")

    def test_category_is_attached(self):
        """每个词要带上它所属的分类（日志与提示语都要用到）。"""
        wl = cf._parse_wordlist("# 广告\n加微信\n# 违法\n赌博\n")
        mapping = dict(zip(wl.words, wl.categories))
        self.assertEqual(mapping["加微信"], "广告")
        self.assertEqual(mapping["赌博"], "违法")


class DetectionTestCase(unittest.TestCase):
    """基础检测：该拦的拦住，不该拦的别误伤。"""

    def test_normal_text_passes(self):
        """普通商品问题必须放行 —— 误拦正常提问比漏拦更伤用户体验。"""
        result = cf.check("星跃 S10 手机的电池容量是多少毫安时？")
        self.assertFalse(result.hit)
        self.assertEqual(result.masked, "星跃 S10 手机的电池容量是多少毫安时？")

    def test_plain_hit(self):
        result = cf.check("你加微信我私聊你")
        self.assertTrue(result.hit)
        self.assertIn("加微信", result.words)

    def test_empty_text_is_safe(self):
        for s in ("", None):
            r = cf.check(s or "")
            self.assertFalse(r.hit)

    # ---------- 花式绕过（重点） ----------

    def test_normalization_view(self):
        """归一化本身要正确：全角转半角、去空白、去干扰符号、统一小写。

        这是整个防绕过的地基 —— 地基歪了，上面所有"能不能识破"的结论都不可信，
        所以直接对归一化结果做逐字断言，而不是只看最终的"命中/不命中"。
        """
        view, positions = cf._build_scan_view("Ｇ ａ-ｂ　Ｃ")
        self.assertEqual(view, "gabc", "归一化结果不对（全角/空格/短横线没处理干净）")
        self.assertEqual(len(positions), len(view), "位置映射长度与归一化文本对不上")
        # 逐位核对映射关系（原文下标）：0=Ｇ、2=ａ、4=ｂ、6=Ｃ
        # 注意别凭印象写常量——这里第一版就写错了（以为 b 在第 5 位），
        # 是断言错、不是程序错。所以下面按原文逐位算清楚再写。
        self.assertEqual(positions, [0, 2, 4, 6], "位置映射错位，打码会打错地方")

    def test_space_between_chars_is_caught(self):
        """中间插空格（"赌 博"）要能识破 —— 这是最常见的绕过手法。"""
        r = cf.check("这个产品可以 赌 博 吗")
        self.assertTrue(r.hit, "插空格就绕过了，等于词表比对失效")
        self.assertIn("赌博", r.words)

    def test_punctuation_between_chars_is_caught(self):
        """中间插符号（"赌-博"、"赌*博"）也要能识破。"""
        for s in ("赌-博", "赌*博", "赌.博", "赌、博"):
            r = cf.check(f"请问{s}违法吗")
            self.assertTrue(r.hit, f"{s} 这种写法绕过了检测")

    def test_fullwidth_letters_are_caught(self):
        """全角字母词（把 abc 写成 ａｂｃ）要能识破。"""
        with temp_wordlist("gamble\n"):
            r = cf.check("我们来 ｇａｍｂｌｅ 一下")
            self.assertTrue(r.hit, "全角字母绕过了检测")

    def test_case_insensitive(self):
        """英文词大小写不敏感。"""
        with temp_wordlist("gamble\n"):
            for s in ("GAMBLE", "Gamble", "gAmBlE"):
                self.assertTrue(cf.check(s).hit, f"{s} 没有被识别")

    # ---------- 打码 ----------

    def test_mask_keeps_length_and_position(self):
        """打码必须"等长且位置正确"。

        如果打码把原文长度改掉了，调用方（比如前端要高亮、或者下游要按位置切片）
        就会全部错位。所以这里逐字符比对：除了命中区间，其余字符必须一模一样。
        """
        text = "请告诉我加微信怎么操作"
        r = cf.check(text)
        self.assertTrue(r.hit)
        self.assertEqual(len(r.masked), len(text), "打码后长度变了")
        # 命中区间之外必须原样保留
        self.assertEqual(r.masked[:4], "请告诉我")
        self.assertEqual(r.masked[-4:], "怎么操作")
        # 命中区间必须全部变成打码字符
        self.assertNotIn("加微信", r.masked)
        self.assertEqual(r.masked[4:4 + 3], "＊＊＊")

    def test_mask_handles_multiple_hits(self):
        """一句话里命中多个词，都要打上码。"""
        text = "加微信 然后 赌博"
        r = cf.check(text)
        self.assertTrue(r.hit)
        self.assertGreaterEqual(len(r.hits), 2)
        self.assertNotIn("加微信", r.masked)
        self.assertNotIn("赌博", r.masked)
        self.assertEqual(len(r.masked), len(text))

    def test_mask_without_hit_returns_original(self):
        text = "A8 扫地机器人适用面积多大？"
        self.assertEqual(cf.mask(text), text)

    def test_categories_reported(self):
        """命中时要能报出分类（用于给用户"涉及哪一类"的提示）。"""
        r = cf.check("加微信详聊")
        self.assertTrue(r.categories)
        self.assertTrue(all(isinstance(c, str) and c for c in r.categories))


class StreamGuardTestCase(unittest.TestCase):
    """流式回答的增量检测：这是最容易漏的一环。"""

    def test_catches_word_split_across_chunks(self):
        """敏感词被切成两半、分别落在两个数据块里，也必须被抓到。

        为什么专门测这个：AI 回答是一小段一小段吐出来的，
        如果只看单块内容，"加微" 好、"信" 好，两边都没命中，
        拼起来是"加微信"却漏过去了——这是流式场景最容易出的漏网之鱼。
        """
        g = cf.StreamGuard()
        self.assertFalse(g.feed("你可以加").hit)
        hit = g.feed("微信详聊")
        self.assertTrue(hit.hit, "跨数据块的敏感词没被抓到（流式场景漏检）")
        self.assertIn("加微信", hit.words)

    def test_word_entirely_inside_one_chunk(self):
        g = cf.StreamGuard()
        self.assertTrue(g.feed("那就加微信吧").hit)

    def test_long_normal_text_streamed_in_pieces_is_not_flagged(self):
        """反向对照：把一大段正常文本切成很多块喂进去，绝不能误报。

        这条是防"把校验写得太宽"——比如缓冲区累积太长后老是命中自己，
        或窗口计算错位导致重复报。
        """
        g = cf.StreamGuard()
        answer = (
            "星跃 S10 采用 6.7 英寸屏幕，电池容量为 5000mAh，支持 66W 快充。"
            "整机重量约 195 克，支持 IP68 防水，适合日常通勤和户外使用。" * 3
        )
        flagged = False
        for i in range(0, len(answer), 7):        # 每 7 个字切一块，故意切得很碎
            if g.feed(answer[i:i + 7]).hit:
                flagged = True
                break
        self.assertFalse(flagged, "正常文本被误判为敏感内容")

    def test_catches_word_at_very_end_after_long_prefix(self):
        """前缀很长、敏感词出现在最后几个字，也不能漏。

        这一条专门盯"缓冲区被裁剪之后会不会把接缝处的词漏掉"——
        长回答里缓冲区会不断裁剪，裁剪点正是最容易漏检的地方。
        """
        g = cf.StreamGuard()
        prefix = "这是一段完全正常的商品介绍文字，用来把缓冲区撑长。" * 5
        for i in range(0, len(prefix), 5):
            self.assertFalse(g.feed(prefix[i:i + 5]).hit)
        # "赌" 单独一个字不成词，不应命中
        self.assertFalse(g.feed("最后我们聊聊赌").hit, "单字被误判了")
        # 接上"博"之后才构成完整词，必须命中
        self.assertTrue(g.feed("博的危害").hit, "长前缀之后的敏感词被漏掉了")

    def test_empty_piece_is_ignored(self):
        g = cf.StreamGuard()
        self.assertFalse(g.feed("").hit)


class EmptyWordListTestCase(unittest.TestCase):
    """词表为空时的行为：不能崩，只是"不拦"。"""

    def test_check_does_not_crash_with_empty_wordlist(self):
        with temp_wordlist("# 全是注释，一个词都没有\n"):
            r = cf.check("赌博加微信")
            self.assertFalse(r.hit)
            self.assertEqual(r.masked, "赌博加微信")

    def test_wordlist_missing_file_does_not_crash(self):
        """词表文件被删/路径写错时，check() 也不能抛异常把问答接口带崩。"""
        with unittest.mock.patch.object(
            cf, "_wordlist_path", return_value=Path("/definitely/not/exist/words.txt")
        ):
            cf.load_wordlist(force=True)
            r = cf.check("随便什么文本")
            self.assertFalse(r.hit, "词表读不到时应当放行，而不是抛异常")
        with unittest.mock.patch.object(cf, "_wordlist_path", return_value=cf._WORDLIST_PATH):
            cf.load_wordlist(force=True)


if __name__ == "__main__":
    unittest.main()
