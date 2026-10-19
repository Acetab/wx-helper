# 成语词表来源

`idioms.txt` 来自 [pwxcoo/chinese-xinhua](https://github.com/pwxcoo/chinese-xinhua)，保留上游 MIT 许可，见同目录 `LICENSE.chinese-xinhua.txt`。只提取词条，不包含释义、例句或出处。

- 固定数据版本：[`8de1001aa499cd65fb97ef8712550188c0297a08/data/idiom.json`](https://github.com/pwxcoo/chinese-xinhua/blob/8de1001aa499cd65fb97ef8712550188c0297a08/data/idiom.json)。
- 源文件 SHA-256：`1d4b4f454ce1c416d6a1ab2369d6e66c0ff99e04390172eef70790499e21ce19`。
- 源文件共 30,895 条记录；只保留四字且全部位于 U+4E00–U+9FFF 的 `word`，去重后按 Unicode 排序，共 29,502 条。每行一条，UTF-8 无 BOM、LF 换行，文件末尾有换行。
- 生成文件 SHA-256：`5e7433805e02ca7d40f3d5caed7b96fb3da5451851a8e73d08cc32239ec63abf`。仓库通过 `.gitattributes` 保持该文件为 LF 换行。

可从下载的固定版本 JSON 复现：

```python
import json
from pathlib import Path

rows = json.loads(Path("idiom.json").read_text(encoding="utf-8"))
words = sorted({row["word"] for row in rows
                if len(row["word"]) == 4
                and all("\u4e00" <= ch <= "\u9fff" for ch in row["word"])})
Path("idioms.txt").write_bytes(("\n".join(words) + "\n").encode("utf-8"))
```

该词表属于收集整理的数据，可能包含异体词和原始错误，不表示穷尽全部成语或逐条人工审定。运行时随插件离线加载；表外玩家答案仍由模型复核。`lexicon.py` 的常用词清单用于保留容易接的落脚字，不再限制机器人只能从常用清单出词。
