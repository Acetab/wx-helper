"""扩展接龙词库与常用接法分开：扩大出题范围，保留容易接的落脚字。

词条为项目维护的简体四字成语清单，不包含复制的词典释义。
新增词条时需同时检查真实性、常用程度以及首尾衔接。
"""
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
import random

WORDS = """
一心一意 一马当先 一帆风顺 一鸣惊人 一举两得 一目了然 一丝不苟 一成不变 一视同仁 一无所有
一往无前 一言为定 一气呵成 一鼓作气 一针见血 一知半解 一五一十 一干二净 一清二白 一清二楚
一穷二白 一窍不通 一见如故 一见钟情 一落千丈 一波三折 一字千金 一石二鸟 一路顺风 一技之长
意气风发 意想不到 意味深长 意气相投
发扬光大 发愤图强 发人深省 发号施令 发奋图强
大显身手 大功告成 大快人心 大吃一惊 大惊小怪 大同小异 大公无私 大失所望 大有可为
大义凛然 大智若愚 大器晚成 大开眼界 大张旗鼓 大海捞针 大言不惭 大庭广众 大刀阔斧
心满意足 心花怒放 心直口快 心平气和 心灵手巧 心惊胆战 心不在焉 心灰意冷
心血来潮 心甘情愿 心有余悸 心猿意马 心旷神怡 心口如一 心安理得 心照不宣 心领神会
成千上万 成群结队 成竹在胸 成人之美 成家立业
万众一心 万无一失 万紫千红 万水千山 万象更新 万里无云 万马奔腾 万古长青
众所周知 众志成城 众口难调 众目睽睽 众星捧月
人山人海 人来人往 人才辈出 人尽其才 人声鼎沸 人浮于事 人云亦云 人杰地灵 人之常情
人定胜天 人心所向 人满为患 人各有志 人情世故 人寿年丰 人面兽心
海阔天空 海底捞针 海市蜃楼 海枯石烂 海纳百川
天长地久 天衣无缝 天南地北 天高地厚 天经地义 天翻地覆 天真烂漫 天伦之乐 天涯海角
天马行空 天罗地网 天花乱坠 天造地设 天荒地老 天壤之别
空前绝后 空穴来风 空口无凭 空空如也
风和日丽 风平浪静 风雨同舟 风吹草动 风起云涌 风调雨顺 风雨无阻 风餐露宿 风尘仆仆
风声鹤唳 风云变幻 风华正茂 风驰电掣 风卷残云 风流倜傥
顺理成章 顺水推舟 顺藤摸瓜
自言自语 自由自在 自力更生 自强不息 自始至终 自相矛盾 自作自受 自知之明 自告奋勇
自得其乐 自以为是 自食其力 自顾不暇 自然而然 自暴自弃 自欺欺人 自圆其说 自命不凡
语重心长 语无伦次 语焉不详
长年累月 长话短说 长驱直入 长此以往 长治久安 长途跋涉
月明星稀 月白风清 月下老人 月圆花好
日新月异 日积月累 日久天长 日理万机 日以继夜 日月如梭 日薄西山 日上三竿 日复一日
新陈代谢 新仇旧恨 新婚燕尔
异口同声 异想天开 异曲同工 异军突起
声东击西 声情并茂 声泪俱下 声势浩大
西装革履 西风残照
下不为例 下落不明 下里巴人 下笔成章 下车伊始
手舞足蹈 手到擒来 手足无措 手忙脚乱 手不释卷 手疾眼快 手无寸铁
来日方长 来之不易 来龙去脉 来者不拒 来势汹汹
水到渠成 水落石出 水深火热 水泄不通 水滴石穿 水涨船高 水乳交融 水天一色 水土不服
出人头地 出类拔萃 出其不意 出生入死 出神入化 出口成章 出奇制胜 出谋划策 出言不逊
地久天长 地大物博 地动山摇 地广人稀 地老天荒
得心应手 得意忘形 得不偿失 得寸进尺 得天独厚 得过且过 得意洋洋 得道多助
失魂落魄 失之交臂 失道寡助
不约而同 不知所措 不言而喻 不可思议 不计其数 不劳而获 不翼而飞 不屈不挠 不慌不忙
不谋而合 不相上下 不遗余力 不择手段 不自量力 不动声色 不闻不问 不可一世 不以为然
不学无术 不欢而散 不速之客 不辞而别 不足为奇 不堪回首 不堪一击 不求甚解 不由自主
同心协力 同甘共苦 同舟共济 同归于尽 同病相怜 同床异梦 同流合污 同日而语
力不从心 力所能及 力挽狂澜 力争上游 力透纸背
生龙活虎 生机勃勃 生生不息 生离死别 生死之交 生花妙笔 生财有道 生灵涂炭
虎头蛇尾 虎视眈眈 虎口余生 虎背熊腰 虎落平阳
龙飞凤舞 龙腾虎跃 龙争虎斗 龙马精神 龙潭虎穴
精益求精 精打细算 精疲力尽 精神抖擞 精诚所至 精兵强将
神采奕奕 神机妙算 神通广大 神出鬼没 神清气爽
尽心尽力 尽善尽美 尽力而为 尽如人意 尽人皆知
美不胜收 美中不足 美轮美奂
足智多谋 足不出户 足不出门
知己知彼 知难而退 知难而进 知足常乐 知法犯法 知人善任 知无不言
乐不思蜀 乐此不疲 乐不可支 乐极生悲 乐在其中 乐善好施
言而有信 言行一致 言之有理 言不由衷 言简意赅 言归正传 言听计从 言过其实
信以为真 信口开河 信手拈来 信誓旦旦
理直气壮 理所当然 理屈词穷
气象万千 气势磅礴 气急败坏 气吞山河 气宇轩昂 气壮山河
千方百计 千辛万苦 千军万马 千言万语 千变万化 千头万绪 千载难逢 千钧一发 千篇一律
千真万确 千奇百怪 千山万水 千家万户 千丝万缕
马到成功 马不停蹄 马马虎虎 马首是瞻
功成名就 功败垂成 功德无量 功亏一篑 功成身退
无忧无虑 无微不至 无边无际 无独有偶 无可奈何 无中生有 无能为力 无地自容 无与伦比
无声无息 无动于衷 无所不知 无所事事 无法无天 无理取闹 无关紧要 无所适从 无奇不有
有口皆碑 有目共睹 有声有色 有始有终 有条不紊 有备无患 有的放矢 有恃无恐 有勇有谋
有求必应 有气无力 有机可乘 有朝一日 有名无实 有始无终
事半功倍 事倍功半 事与愿违 事在人为 事出有因 事不宜迟 事过境迁
为所欲为 为人师表 为虎作伥 为富不仁 为民除害
前功尽弃 前赴后继 前所未有 前因后果 前车之鉴 前仆后继
后发制人 后来居上 后顾之忧 后起之秀 后继有人
上行下效 上下一心 上天入地
入木三分 入乡随俗 入情入理 入不敷出
分秒必争 分道扬镳 分庭抗礼 分崩离析
争先恐后 争分夺秒 争奇斗艳 争风吃醋
花好月圆 花言巧语 花团锦簇 花枝招展 花天酒地 花容月貌 花红柳绿
好事多磨 好高骛远 好景不长 好逸恶劳 好为人师 好大喜功
名列前茅 名副其实 名落孙山 名正言顺 名不虚传 名存实亡 名扬四海
实事求是 实至名归
山清水秀 山穷水尽 山高水长 山崩地裂 山珍海味 山明水秀
明目张胆 明知故问 明辨是非 明争暗斗 明哲保身 明察秋毫
身临其境 身不由己 身先士卒 身经百战 身体力行 身败名裂 身无分文
文质彬彬 文不对题 文过饰非 文武双全
全心全意 全力以赴 全神贯注 全军覆没
两全其美 两败俱伤 两袖清风 两小无猜
十全十美 十拿九稳 十万火急 十年寒窗
急中生智 急功近利 急于求成 急不可待 急流勇退
智勇双全
见多识广 见义勇为 见异思迁 见机行事 见微知著 见利忘义 见风使舵 见贤思齐
广开言路 广结良缘 广厦万间
路不拾遗 路见不平 路人皆知
平易近人 平心静气 平步青云 平分秋色 平安无事
色厉内荏
百发百中 百折不挠 百依百顺 百思不解 百战百胜 百花齐放 百年好合 百无聊赖 百感交集
中流砥柱 中庸之道
道听途说 道貌岸然 道不拾遗
说一不二 说三道四 说长道短 说来话长
四海为家 四面八方 四通八达 四面楚歌 四平八稳
家喻户晓 家常便饭 家徒四壁 家破人亡 家学渊源
方兴未艾 方寸已乱
三心二意 三思而行 三言两语 三长两短 三头六臂 三顾茅庐 三令五申 三番五次
行云流水 行之有效 行尸走肉 行色匆匆
走马观花 走投无路 走南闯北
白手起家 白日做梦 白驹过隙 白头偕老 白纸黑字
老当益壮 老生常谈 老马识途 老谋深算 老态龙钟 老骥伏枥
金玉良言 金碧辉煌 金蝉脱壳 金榜题名 金科玉律 金口玉言 金戈铁马 金枝玉叶
叶公好龙 叶落归根
根深蒂固 根深叶茂
画蛇添足 画龙点睛 画地为牢 画饼充饥
雪中送炭 雪上加霜 雪泥鸿爪
冰天雪地 冰清玉洁 冰雪聪明 冰消瓦解
解囊相助 解甲归田
助人为乐 助纣为虐
安居乐业 安然无恙 安分守己 安步当车 安土重迁
业精于勤
开门见山 开天辟地 开诚布公 开源节流 开卷有益 开花结果
公正无私 公事公办 公而忘私
流连忘返 流言蜚语 流芳百世 流离失所 流水无情
世外桃源 世态炎凉 世风日下
情不自禁 情投意合 情同手足 情有可原 情深义重 情真意切
重见天日 重蹈覆辙 重整旗鼓 重于泰山
鼓舞人心
青出于蓝 青梅竹马 青云直上 青山绿水
绿水青山 绿林好汉
光明正大 光明磊落 光彩夺目 光怪陆离 光阴似箭
目不转睛 目瞪口呆 目中无人 目不暇接 目不识丁
接二连三 接连不断 接踵而至
至高无上 至善至美 至理名言
别出心裁 别开生面 别具一格 别有洞天 别有用心
面面俱到 面红耳赤 面目全非 面不改色 面面相觑


朝三暮四 朝气蓬勃 朝思暮想 朝秦暮楚
想方设法 想入非非

恩重如山 恩将仇报 恩怨分明
报仇雪恨
真心实意 真才实学 真知灼见 真相大白 真情实感
学以致用 学富五车 学无止境 学而不厌
用心良苦 用武之地 用兵如神
苦尽甘来 苦口婆心 苦中作乐 苦心孤诣 苦不堪言
快人快语 快马加鞭
如鱼得水 如虎添翼 如火如荼 如梦初醒 如愿以偿 如释重负 如数家珍 如日中天 如出一辙
鱼目混珠 鱼龙混杂 鱼贯而入
火上浇油 火冒三丈 火烧眉毛 火树银花
投其所好 投机取巧 投桃报李 投鼠忌器
巧夺天工 巧立名目 巧言令色
物极必反 物尽其用 物是人非 物以类聚
反败为胜 反客为主 反复无常 反目成仇
胜友如云
云开见日 云淡风轻 云消雾散
轻而易举 轻车熟路 轻描淡写 轻举妄动 轻重缓急
举一反三 举世闻名 举足轻重 举棋不定 举手之劳
定国安邦
良师益友 良药苦口 良莠不齐 良辰美景
口是心非 口若悬河 口口声声 口蜜腹剑 口干舌燥 口诛笔伐
非同小可 非亲非故
可想而知 可歌可泣 可乘之机 可有可无
机不可失 机不容发
才高八斗 才华横溢 才疏学浅 才貌双全
斗志昂扬 斗转星移
扬眉吐气 扬长避短 扬长而去
去伪存真 去粗取精
惊天动地 惊弓之鸟 惊心动魄 惊涛骇浪 惊慌失措
鸟语花香 鸟尽弓藏

落井下石 落花流水 落落大方 落叶归根 落荒而逃
石破天惊 石沉大海
针锋相对
对答如流 对牛弹琴 对症下药
药到病除
除旧布新 除暴安良
现身说法
问心无愧 问长问短 问道于盲
短兵相接 短小精悍
兵不厌诈 兵临城下 兵荒马乱 兵强马壮
壮志凌云 壮志未酬
魂不守舍 魂牵梦萦
舍己为人 舍生取义 舍近求远 舍本逐末
义不容辞 义无反顾 义正词严
辞不达意
顾全大局 顾名思义 顾此失彼
局促不安
安之若素
素不相识
识途老马
持之以恒 持之有故
故弄玄虚 故步自封 故作高深
深思熟虑 深入浅出 深明大义 深谋远虑 深不可测 深信不疑
疑神疑鬼 疑难杂症
鬼斧神工 鬼使神差 鬼迷心窍
差强人意

胸有成竹 胸怀大志 胸无点墨
志同道合 志在四方 志得意满 志大才疏
合情合理 合二为一
满载而归 满面春风 满城风雨 满腹经纶
归心似箭 归根结底 归根到底
箭在弦上
""".split()


@lru_cache(maxsize=1)
def dictionary_words():
    """发布包内置的固定版本词表；运行时不下载，也不依赖模型补词。"""
    return frozenset(Path(__file__).with_name("data").joinpath("idioms.txt").read_text(
        encoding="utf-8").split())


class Lexicon:
    def __init__(self, words=None, *, common_words=None, rng=None):
        # 显式传入小词表时保持独立，便于测试和自定义词库。
        supplied = words is not None
        self.words = frozenset(words) if supplied else dictionary_words() | frozenset(WORDS)
        self.common_words = self.words & frozenset(
            common_words if common_words is not None else self.words if supplied else WORDS)
        self.by_first = defaultdict(list)
        self.common_by_first = defaultdict(list)
        for word in sorted(self.words):
            self.by_first[word[0]].append(word)
            if word in self.common_words:
                self.common_by_first[word[0]].append(word)
        self.rng = rng or random.Random()

    def continuations(self, previous, used):
        blocked = set(used) | {previous}
        return [word for word in self.by_first.get(previous[-1], ()) if word not in blocked]

    def suggestions(self, previous, used):
        """提示和参考答案先显示常用词，避免大词库的字典序把生僻词排在前面。"""
        choices = self.continuations(previous, used)
        return sorted(choices, key=lambda word: (word not in self.common_words, word))

    def playable(self, words, used, minimum=3):
        blocked = set(used)
        counts = {char: sum(word not in blocked for word in entries)
                  for char, entries in self.common_by_first.items()}
        return [word for word in words if word not in blocked and
                counts.get(word[-1], 0) - int(word in self.common_words and word[0] == word[-1]) >= minimum]

    def choose(self, previous, used, minimum=3):
        """优先留足 minimum 个接法；不足时逐级放宽到 1，避免把可接的题判成死路。"""
        candidates = self.continuations(previous, used)
        for floor in range(minimum, 0, -1):
            options = self.playable(candidates, used, floor)
            if options:
                return self._choose_sustainable(options, used, floor)
        return ""

    def opening(self, minimum=3, *, used=()):
        for floor in range(minimum, 0, -1):
            candidates = self.playable(sorted(self.words), used, floor)
            if candidates:
                return self._choose_sustainable(candidates, used, floor)
        raise ValueError("词库没有留有常用接法的开场成语")

    def _choose_sustainable(self, candidates, used, minimum):
        """以常用接法估计后续可玩性；按首尾字聚合，避免大词库逐词多层扫描。"""
        blocked = set(used)
        replies_by_first = {char: [word for word in entries if word not in blocked]
                            for char, entries in self.common_by_first.items()}
        sustainable_starts = {word[0] for word in self.playable(self.words, blocked, minimum)}
        # 这是选词偏好，不是后续所有路径的保证；真正出题仍逐次检查未使用的常用接法。
        ratios = {}
        for char, replies in replies_by_first.items():
            ratios[char] = sum(reply[-1] in sustainable_starts for reply in replies) / len(replies) if replies else 0
        scored = []
        for word in candidates:
            scored.append((ratios.get(word[-1], 0), word))
        best = max(score for score, _ in scored)
        options = [word for score, word in scored if score >= best - 0.1]
        common = [word for word in options if word in self.common_words]
        expanded = [word for word in options if word not in self.common_words]
        # 熟词仍占多数，也让新增词条能出场；熟词接不上时可直接使用扩展词库。
        pool = common if common and (not expanded or self.rng.random() < 0.75) else expanded
        return self.rng.choice(pool)
