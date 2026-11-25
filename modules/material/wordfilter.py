# -*- coding: utf-8 -*-
"""违规词宽松匹配(归一化后再做子串匹配)
=========================================
老逻辑是 `词 in 文案` 的严格子串匹配, 抖音文案里常见的写法会漏杀:
    "测 评"  "测-评"  "测·评"  "测/评"  "【测评】"  "測評"(繁体)  "测\u200b评"(零宽)
本模块把文案和词表都做同一套归一化, 再做子串匹配:

    1) unicodedata NFKC  -> 全角转半角(ａ→a １→1), 兼容字符归一(⽂→文)
    2) 转小写
    3) 繁转简 (有 zhconv/opencc 就用来转换, 否则用内置常用对照表)
    4) 只保留 汉字/字母/数字 -> 空格、标点、符号、emoji、零宽字符全部去掉

因此 "测 评" "测-评" "【测·評】" 都能命中词表里的 "测评"。
代价: 极端情况下符号两侧的两个字会被拼成违规词(如 "买个好测…评分"), 属可接受的误杀。

用法:
    from wordfilter import prepare_words, hit_banned
    ws = prepare_words(words)          # 词表 -> [(归一化词, 原词)]
    hits = hit_banned("文案 作者", ws)  # -> 命中的原词列表

环境变量 XP_STRICT_WORDS=1 可退回老的严格子串匹配(排查误杀时用)。
单独自测: python wordfilter.py "这是一段文案 #话题 @作者"
"""
import os
import re
import unicodedata

STRICT = os.environ.get("XP_STRICT_WORDS", "").strip().lower() in ("1", "true", "yes", "on")

# ---- 繁 -> 简 常用对照(内置, 不依赖第三方库) ----
_TRAD_SIMP_PAIRS = """
測测 評评 國国 說说 會会 後后 裡里 裏里 來来 時时 開开 關关 門门 問问 間间 東东
車车 馬马 鳥鸟 魚鱼 龍龙 買买 賣卖 錢钱 價价 貨货 質质 貴贵 費费 賺赚 點点 數数
據据 網网 頁页 號号 碼码 單单 雙双 們们 個个 這这 麼么 樣样 種种 對对 於于 為为
與与 無无 還还 進进 過过 邊边 長长 陽阳 陰阴 風风 雲云 電电 話话 視视 聽听 讀读
寫写 書书 筆笔 紙纸 學学 師师 醫医 藥药 廠厂 產产 業业 實实 現现 發发 髮发 變变
讓让 覺觉 認认 識识 記记 憶忆 準准 備备 簡简 複复 雜杂 難难 輕轻 鬆松 緊紧 張张
彈弹 強强 壯壮 麗丽 華华 豐丰 滿满 溫温 熱热 凍冻 濕湿 幹干 淨净 髒脏 醜丑 帥帅
顏颜 頭头 臉脸 膚肤 護护 妝妆 飾饰 裝装 褲裤 襪袜 帶带 領领 內内 側侧 層层 壓压
縮缩 擴扩 寬宽 軟软 鐵铁 鋼钢 銅铜 銀银 鋁铝 線线 絲丝 繩绳 織织 縫缝 補补 貼贴
膠胶 劑剂 噴喷 塗涂 灑洒 掃扫 養养 調调 節节 適适 營营 銷销 購购 選选 擇择 換换
遞递 運运 輸输 達达 郵邮 庫库 盤盘 賬账 帳账 稅税 報报 錶表 鐘钟 萬万 億亿 兩两
幾几 條条 塊块 顆颗 隻只 組组 隊队 眾众 兒儿 媽妈 爺爷 孫孙 婦妇 親亲 愛爱 戀恋
歡欢 樂乐 傷伤 癢痒 腫肿 紅红 綠绿 藍蓝 黃黄 閃闪 潤润 澀涩 膩腻 鹹咸 鮮鲜 韌韧
齊齐 濟济 廣广 慶庆 應应 態态 惡恶 擾扰 撫抚 擊击 擔担 擺摆 攝摄 敗败 敵敌 斷断
機机 檢检 權权 歐欧 殘残 殺杀 氣气 決决 沒没 淚泪 潔洁 濃浓 灣湾 煩烦 燈灯 燒烧
牆墙 獨独 環环 獎奖 療疗 盡尽 監监 確确 礎础 禮礼 窮穷 競竞 範范 籃篮 糧粮 紀纪
約约 級级 純纯 納纳 細细 終终 結结 絕绝 統统 給给 經经 維维 練练 緩缓 總总 績绩
續续 罰罚 義义 習习 聯联 職职 腦脑 腳脚 腸肠 臟脏 臨临 興兴 舉举 舊旧 蓋盖 蘋苹
蟲虫 蠶蚕 術术 衛卫 製制 規规 觀观 訂订 計计 訓训 討讨 訪访 許许 診诊 註注 詞词
試试 詩诗 詳详 語语 誠诚 誰谁 課课 論论 談谈 請请 諾诺 謀谋 證证 譯译 豬猪 貓猫
貝贝 負负 財财 貢贡 貧贫 責责 販贩 貪贪 貫贯 賞赏 賠赔 賢贤 賴赖 贈赠 贊赞 趕赶
趨趋 跡迹 踐践 蹤踪 躍跃 場场 塵尘 墊垫 墳坟 壞坏 壩坝 壽寿 夢梦 夠够 奮奋 嬰婴
寧宁 寶宝 審审 宮宫 產产 噴喷 嚐尝 麼么 嗎吗 喲哟 嘍喽 餵喂 麵面 嚮向 響响 鬱郁
"""


def _build_table():
    table = {}
    for pair in _TRAD_SIMP_PAIRS.split():
        if len(pair) != 2:
            continue
        t, s = pair[0], pair[1]
        if t != s:
            table[ord(t)] = s
    return table


_T2S = _build_table()
_ZHCONV = None


def _init_zhconv():
    """装了 zhconv / opencc 就用它们(繁简覆盖更全), 否则返回 None"""
    try:
        import zhconv
        return lambda s: zhconv.convert(s, "zh-cn")
    except Exception:
        pass
    try:
        from opencc import OpenCC
        cc = OpenCC("t2s")
        return cc.convert
    except Exception:
        return None


_ZHCONV = _init_zhconv()

_HAN = "\u3400-\u4dbf\u4e00-\u9fff"
_KEEP = re.compile(rf"[^0-9a-z{_HAN}]")


def normalize(text):
    """宽松归一: NFKC + 小写 + 繁转简 + 只留汉字/字母/数字"""
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = s.lower()
    if _ZHCONV is not None:
        try:
            s = _ZHCONV(s)
        except Exception:
            s = s.translate(_T2S)
    else:
        s = s.translate(_T2S)
    return _KEEP.sub("", s)


def prepare_words(words, strict=None):
    """词表 -> [(归一化词, 原词)]; 归一化后为空的词(纯符号/emoji)丢弃"""
    strict = STRICT if strict is None else strict
    out, seen = [], set()
    for w in words or []:
        orig = str(w).strip()
        if not orig:
            continue
        n = orig.upper() if strict else normalize(orig)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append((n, orig))
    return out


def hit_banned(text, prepared, strict=None):
    """返回命中的违规词(原词形式); prepared = prepare_words() 的结果"""
    strict = STRICT if strict is None else strict
    if not prepared:
        return []
    t = str(text or "").upper() if strict else normalize(text)
    if not t:
        return []
    return [orig for n, orig in prepared if n and n in t]


def explain(text, words):
    """自测用: 打印归一化文本和每个词的命中情况"""
    strict = STRICT
    print(f"模式: {'严格(原样子串)' if strict else '宽松(归一化后子串)'}")
    if not strict:
        print(f"归一化文案: {normalize(text)}")
    for n, orig in prepare_words(words, strict=strict):
        if n in (text.upper() if strict else normalize(text)):
            print(f"  命中: {orig}  (归一化: {n})")
    return 0


if __name__ == "__main__":
    import sys
    sample = sys.argv[1] if len(sys.argv) > 1 else \
        "这件太好用了【测 评】測評实拍 #好物分享 @小助手"
    demo = ["测评", "好物", "实测", "假一赔十", "美白"]
    explain(sample, demo)
