"""eval/deterministic.py 的单元测试 —— 零 API key、零网络，CI 里能稳定跑绿。

## 测试策略：一份"全过"的基准稿 + 每次只破坏一项
`_good_blog()` 造出一份能通过全部检查的稿子作为基准。每个测试拿它改**一处**，
断言「恰好那一项变红、其余仍然绿」。这样做的好处是：某项检查写得过宽（把不该拦的拦了），
会在别的测试里以"多了一条 failure"的形式暴露出来，而不是悄悄放过。

## 为什么这些测试值钱（不只是覆盖率）
① 它们是 prompt 回归测试的地基 —— 阈值改动、正则改动的后果立刻可见；
② 它们**不需要 API key**，所以能真的进 CI 并且一直绿（跑真模型的测试注定会被跳过）；
③ 里面有两条测的是**隐含假设**而不是我们自己的代码逻辑
   （bs4 不把 HTML 注释算作正文、"lifetime warranty" 不被硬禁词拦），
   哪天依赖或规则变了，这两条会第一时间报警。
"""

import pytest

from eval.deterministic import check_seo

WEBSITE = "https://harborautospa.example/"
SLOTS = ("hero", "mid", "end")

TOPIC = {
    "primary_keyword": "ceramic coating Lakeview",
    "secondary_keywords": ["paint protection film", "window tint"],
}


def _excerpt(n: int = 155) -> str:
    """造一个**恰好 n 个字符**的摘要（默认 155，落在 150-160 区间正中）。

    刻意用"重复裁剪"而不是手写一段话：手写的字符串一改字就会漂出区间，
    测试会开始因为无关原因红 —— 而这个 helper 让"长度"变成一个可控的参数。
    """
    base = (
        "Ceramic coating Lakeview drivers trust: what real paint protection costs, "
        "how long it lasts, and how to pick a certified installer near you. "
    )
    s = (base * 4)[:n]
    # 裁剪点可能正好落在空格上，而校验器数的是 strip() 之后的长度 —— 那样就会差 1 个字符，
    # 让本该测"长度"的用例因为空白问题红掉。把尾部空格换成句点，保证 strip 后仍是恰好 n 个。
    if s.endswith(" "):
        s = s[:-1] + "."
    return s


def _good_blog(**overrides) -> dict:
    """一份通过全部检查的基准稿。overrides 用来定点破坏某个字段。

    每个元素都是为了满足某一条检查而存在的，改动前先看清它在为哪条服务：
      · 开篇第一句带主关键词          → 第 2 条（前 100 词）
      · 5 个 <h2>（含 FAQ 那节）      → 第 3 条（4-7 个）
      · 两个副关键词各出现一次        → 第 5 条
      · 摘要 155 字                   → 第 6 条（150-160）
      · 3 条内链（相对 + 绝对各有）   → 第 7 条（3-5 条）
      · 1 条外链（政府站）            → 第 8 条（1-2 条）
      · 3 个严格格式的占位符          → 不变式（== SLOTS）
      · 全 ASCII、无 <img>、无绝对化承诺 → 第 10 条 / 图片契约 / A 类硬禁词
    """
    blog = {
        "title": "Ceramic Coating Lakeview: A Practical Owner's Guide",
        "excerpt": _excerpt(155),
        "image_alts": {
            "hero": "Ceramic coating Lakeview technician polishing a car hood",
            "mid": "Paint protection film applied to a front bumper",
            "end": "Finished vehicle with window tint in a clean shop",
        },
        "content_html": """
<p>Ceramic coating Lakeview owners ask about it first, and for good reason: it changes
how often you wash the car and how the paint looks after three summers.</p>
<!-- BLOG_IMAGE:hero -->
<h2>How ceramic coating compares with paint protection film</h2>
<p>A coating is a chemical layer; paint protection film is a physical one. Read our
<a href="/services/">services overview</a> to see which one fits your driving.</p>
<h2>What window tint adds to the package</h2>
<p>Window tint reduces interior heat and slows dashboard fading. See the
<a href="https://harborautospa.example/about/">shop background</a> for certifications.</p>
<!-- BLOG_IMAGE:mid -->
<h2>How long a coating realistically lasts in Southern California</h2>
<p>Expect meaningful hydrophobic behavior for two to four years with regular
maintenance washes, according to
<a href="https://www.epa.gov/">published environmental guidance</a>.</p>
<h2>What it costs and how to book</h2>
<p>Pricing depends on paint condition and panel count. Use our
<a href="/contact/">contact page</a> for a walk-through quote.</p>
<!-- BLOG_IMAGE:end -->
<div class="faq-section">
  <h2>Frequently Asked Questions</h2>
  <div class="faq-item"><h3>Does a coating remove scratches?</h3>
  <p>No. It adds a sacrificial layer; correction happens before application.</p></div>
</div>
""",
    }
    blog.update(overrides)
    return blog


def _run(blog: dict, topic: dict | None = None):
    """跑一次检查，返回 report。集中在这里方便统一改参数。"""
    return check_seo(blog, topic if topic is not None else TOPIC,
                     website=WEBSITE, expected_slots=SLOTS)


def _failed_names(report) -> set[str]:
    """把 report 里没过的**检查项 ID**收成集合，断言时比字符串可靠。"""
    return {c.name for c in report.checks if not c.ok}


# ── 基准：全过 ────────────────────────────────────────────────────────


def test_severity_table_covers_every_check():
    """★ 防腐测试 ★ 每个检查项都必须在 _SEVERITY 表里有明确分级。

    为什么值得单独一条测试：`check_seo` 对查不到的项兜底给 "hard"（宁可误拦不可误放），
    所以漏登记**不会报错**，只会悄悄多拦一项。这条测试把"悄悄"变成"立刻红"。
    """
    from eval.deterministic import _SEVERITY

    names = {c.name for c in _run(_good_blog()).checks}
    missing = names - set(_SEVERITY)
    extra = set(_SEVERITY) - names
    assert not missing, f"这些检查项没在 _SEVERITY 里分级（会被兜底当 hard）：{missing}"
    assert not extra, f"_SEVERITY 里有已经不存在的检查项，该清理：{extra}"
    assert set(_SEVERITY.values()) <= {"hard", "soft"}


def test_hard_checks_are_the_break_things_ones():
    """分级判据的固化：hard = 会产出坏的/有风险的东西；soft = 只是不够好。

    这条测试的意义不是"检查代码写对了"，而是**把产品决策钉在版本库里** ——
    以后谁想把 internal_links 提成 hard（= 允许它触发 $0.028 的整篇重写），
    必须先改这条断言，也就必须先看到这个取舍。
    """
    from eval.deterministic import _SEVERITY

    assert {k for k, v in _SEVERITY.items() if v == "hard"} == {
        "no_h1",               # 标题层级混乱，肉眼看不出来
        "no_img_tags",         # 碎图，用户直接看到坏东西
        "image_placeholders",  # 图片不出现/重复/注释裸露
        "no_emoji",            # 破坏渲染和 TTS
        "forbidden_claims",    # 品牌安全/虚假宣传风险
        "brand_voice_headline",  # 标题/摘要/小标题 = 品牌门面，零容忍
        "catalog_grounding",     # 编造价格 = 客服问题/信任损失，最该 hard 的一条
    }


def test_good_blog_passes_everything():
    report = _run(_good_blog())
    # 断言里把 failures 打出来：一旦基准稿破了，报错信息直接告诉你破在哪条，
    # 不用再手动跑一遍去看。
    assert report.hard_ok, f"基准稿应该全过，实际未通过：{report.failures}"
    assert report.failures == []
    assert len(report.passed_checks) == len(report.checks)


def test_metrics_are_reported_for_golden_assertions():
    """metrics 是给 golden test 断言用的原始数值，字段和数值都要稳定。"""
    m = _run(_good_blog()).metrics
    assert m["excerpt_chars"] == 155
    assert m["h2_count"] == 5          # 4 个内容章节 + FAQ 那节
    assert m["internal_links"] == 3
    assert m["external_links"] == 1
    assert m["no_h1"] == 0
    assert m["no_img_tags"] == 0
    assert m["body_word_count"] > 100  # 前 100 词那项检查才有意义


# ── 逐项破坏 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("length", [120, 149, 161, 220])
def test_excerpt_length_out_of_range_is_soft(length):
    """摘要长度不在区间 → 报告出来，但**不阻断**（severity=soft）。

    2026-07-27 从 soft 改级的实测理由：首篇实测稿最后卡在「161 字符（超 1 个）」上，
    为这 1 个字符重写了两遍整篇、烧掉 $0.055。摘要没落进区间是"不够好"，
    不是"坏的" —— 何况超长现在已经由 eval/repair.py 自动裁掉了。
    """
    report = _run(_good_blog(excerpt=_excerpt(length)))
    assert report.hard_ok, "摘要长度是 soft，不该阻断出稿"
    assert _failed_names(report) == {"excerpt_length"}
    assert report.soft_failures == report.failures      # 唯一一条失败，且是 soft
    assert report.hard_failures == []
    # 失败文案必须带真实数字 —— 这是"定点指令"的最低要求
    assert str(length) in report.failures[0]


def test_h1_in_body_is_hard_fail():
    blog = _good_blog()
    blog["content_html"] = blog["content_html"].replace(
        "<h2>What it costs and how to book</h2>",
        "<h1>What it costs and how to book</h1>",
    )
    report = _run(blog)
    # 注意这里期望**两项**都红：h1 多了一个，同时 h2 少了一个（5→4 仍在区间内，所以只 h1 红）
    assert _failed_names(report) == {"no_h1"}


def test_h2_count_too_many():
    blog = _good_blog()
    blog["content_html"] += "<h2>Extra section one</h2><h2>Extra section two</h2>" \
                            "<h2>Extra section three</h2>"
    report = _run(blog)
    assert _failed_names(report) == {"h2_count"}
    assert "8" in report.failures[0]      # 5 + 3 = 8 > 上限 7


def test_h2_keyword_stuffing_is_caught():
    """★ 真实案例回归 ★ 四个 h2 用同义动词换皮、共享同一个关键词内核。

    2026-07-27 第二篇实测稿的真实产出（这里是简化版）：
        How to Know Your T Shirt Size Male / How to Get Your T Shirt Size /
        How to Figure Out T Shirt Size / How to Find Your T Shirt Size for Any Brand
    确定性层当时没抓到（只数了数量），reviewer 也给了 88 分放行。
    """
    blog = _good_blog()
    blog["content_html"] = blog["content_html"].replace(
        "<h2>How ceramic coating compares with paint protection film</h2>",
        "<h2>How to know your ceramic coating cost</h2>",
    ).replace(
        "<h2>What window tint adds to the package</h2>",
        "<h2>How to get your ceramic coating cost estimate</h2>",
    ).replace(
        "<h2>How long a coating realistically lasts in Southern California</h2>",
        "<h2>How to figure out ceramic coating cost by panel</h2>",
    )
    report = _run(blog)
    assert "h2_keyword_stuffing" in _failed_names(report)
    hit = next(c for c in report.checks if c.name == "h2_keyword_stuffing")
    assert "ceramic coating cost" in hit.detail
    assert "3 h2" in hit.detail
    # soft 档：读起来蠢但文章能发，不该阻断
    assert hit.severity == "soft"
    assert report.hard_ok


def test_h2_sharing_a_phrase_twice_is_allowed():
    """★ 假阳性防线 ★ 两个 h2 呼应同一个关键词是**期望行为**，不是堆砌。

    SEO 十条第 3 条本来就要求每个 h2 含副关键词。阈值定在"最多 2 个共享"，
    就是为了允许呼应、只拦换皮重复 —— 定成 0 容忍的话这个检查第一天就会被关掉。
    """
    blog = _good_blog()
    blog["content_html"] = blog["content_html"].replace(
        "<h2>What window tint adds to the package</h2>",
        "<h2>How ceramic coating compares with vinyl wrap</h2>",   # 与第 1 个 h2 共享 3 词短语
    )
    report = _run(blog)
    assert "h2_keyword_stuffing" not in _failed_names(report)


def test_h2_stuffing_counts_singular_and_plural_as_one_phrase():
    """★ 第二次真实漏检的回归测试 ★ 单复数不能把重复计数劈成两半。

    2026-07-27 实测：一篇真稿子里
        'fitting t shirt'  出现在 2 个 h2
        'fitting t shirts' 出现在 2 个 h2
    合起来 4 个标题都在说同一件事，**但两边各 2 个、都正好卡在"最多允许 2"的阈值上，
    于是一条都没报** —— 检查形同虚设。修法是在归一化时把简单复数收成单数。
    """
    blog = _good_blog()
    blog["content_html"] = blog["content_html"].replace(
        "<h2>How ceramic coating compares with paint protection film</h2>",
        "<h2>What makes a good ceramic coating package</h2>",
    ).replace(
        "<h2>What window tint adds to the package</h2>",
        "<h2>Picking the best ceramic coating packages</h2>",      # 复数！
    ).replace(
        "<h2>How long a coating realistically lasts in Southern California</h2>",
        "<h2>Comparing ceramic coating package prices</h2>",
    )
    report = _run(blog)
    hit = next(c for c in report.checks if c.name == "h2_keyword_stuffing")
    assert not hit.ok, "单数 package / 复数 packages 必须算同一个短语"
    assert "ceramic coating package" in hit.detail
    assert "3 h2" in hit.detail


@pytest.mark.parametrize("word,expected", [
    ("shirts", "shirt"), ("tees", "tee"), ("sizes", "size"), ("details", "detail"),
    # 这四个不是复数，不能被切坏（都是真实会出现在标题里的英文词）
    ("business", "business"), ("status", "status"),
    ("analysis", "analysis"), ("canvas", "canvas"),
])
def test_singularize_is_conservative(word, expected):
    """只处理简单复数，**不引入完整词干还原** ——

    完整 stemming 会把 "fitting"→"fit"、"matching"→"match"，那是**过度合并**：
    'best fitting t' 和 'best fit t' 本来是不同的表达。
    我们要抓的是"同一个名词短语反复出现"，名词的变形主要就是复数。
    """
    from eval.deterministic import _singularize
    assert _singularize(word) == expected


def test_h2_stuffing_skipped_when_too_few_headings():
    """h2 太少（≤2）时没法判断重复 → 明确跳过，不假装检查过。"""
    blog = _good_blog(content_html="<h2>Only one heading</h2><p>Body text here.</p>")
    report = check_seo(blog, {}, website=WEBSITE, expected_slots=None)
    hit = next(c for c in report.checks if c.name == "h2_keyword_stuffing")
    assert hit.ok and "Skipped" in hit.detail and "1 h2" in hit.detail


def test_missing_secondary_keyword_names_the_word():
    """副关键词没出现时，失败文案里必须**点名是哪个词** —— 否则没法定点修。"""
    topic = dict(TOPIC, secondary_keywords=["paint protection film", "headlight restoration"])
    report = _run(_good_blog(), topic=topic)
    assert _failed_names(report) == {"secondary_keywords"}
    assert "headlight restoration" in report.failures[0]
    assert "paint protection film" not in report.failures[0]   # 出现过的不该被点名


def test_secondary_keyword_plural_still_counts():
    """"window tints"（复数）应该算 "window tint" 出现过 —— 宽松匹配的意义就在这。"""
    blog = _good_blog()
    blog["content_html"] = blog["content_html"].replace("Window tint reduces", "Window tints reduce")
    report = _run(blog)
    assert report.hard_ok, report.failures


def test_primary_keyword_too_late():
    """主关键词全文有、但不在前 100 词 → 失败文案要能区分这两种病。"""
    blog = _good_blog()
    # 把开篇那句里的主关键词抹掉，只在**全文末尾**再放一次 ——
    # 注意不能放在第二个 h2 那种位置：那里还在前 100 词以内，检查会照常通过，
    # 用例就测不到想测的东西了（第一版就是这么写错的）。
    blog["content_html"] = blog["content_html"].replace(
        "Ceramic coating Lakeview owners ask about it first",
        "Owners ask about surface protection first",
    )
    blog["content_html"] += "<p>Book a ceramic coating Lakeview appointment when you are ready.</p>"
    report = _run(blog)
    assert _failed_names(report) == {"primary_keyword_early"}
    assert "too far down" in report.failures[0]


def test_too_few_internal_links():
    blog = _good_blog()
    blog["content_html"] = blog["content_html"].replace('<a href="/contact/">contact page</a>',
                                                        "contact page")
    blog["content_html"] = blog["content_html"].replace('<a href="/services/">services overview</a>',
                                                        "services overview")
    report = _run(blog)
    assert _failed_names(report) == {"internal_links"}


def test_anchor_and_mailto_do_not_count_as_internal_links():
    """#锚点 和 mailto: 不能被算成内链 —— 否则"内链 3-5 条"这条永远轻松达标。"""
    blog = _good_blog()
    blog["content_html"] += ('<p><a href="#top">back to top</a>'
                            '<a href="mailto:hi@harborautospa.example">email us</a></p>')
    report = _run(blog)
    assert report.metrics["internal_links"] == 3    # 仍然是 3，不是 5
    assert report.hard_ok, report.failures


def test_subdomain_counts_as_internal():
    """blog.harborautospa.example 属于本站 → 算内链。"""
    blog = _good_blog()
    blog["content_html"] = blog["content_html"].replace(
        'href="https://www.epa.gov/"', 'href="https://blog.harborautospa.example/guide/"'
    )
    report = _run(blog)
    assert report.metrics["internal_links"] == 4
    # 外链被换掉了 → 外链 0 条，应该只有这一项红
    assert _failed_names(report) == {"external_links"}


def test_www_prefix_is_normalized():
    """商家配 https://harborautospa.example/，文章写 www.harborautospa.example → 仍算内链。"""
    blog = _good_blog()
    blog["content_html"] = blog["content_html"].replace(
        'href="https://harborautospa.example/about/"', 'href="https://www.harborautospa.example/about/"'
    )
    report = _run(blog)
    assert report.metrics["internal_links"] == 3
    assert report.hard_ok, report.failures


def test_emoji_anywhere_is_hard_fail():
    """emoji 出现在任何会被渲染的字段都要拦 —— 这里测的是 image_alts（最容易漏的地方）。"""
    blog = _good_blog()
    blog["image_alts"]["hero"] = "Shiny car 🚗 after coating"
    report = _run(blog)
    assert _failed_names(report) == {"no_emoji"}
    assert "image_alts.hero" in report.failures[0]


def test_em_dash_and_typography_are_not_emoji():
    """排版符号（— ' … °）是合法正文用字，不能被 emoji 检查误伤。"""
    blog = _good_blog(title="Ceramic Coating Lakeview — the owner's guide… 25°C cure")
    report = _run(blog)
    assert report.hard_ok, report.failures


def test_img_tag_is_hard_fail():
    blog = _good_blog()
    blog["content_html"] += '<p><img src="/images/hero.jpg" alt="hero"></p>'
    report = _run(blog)
    assert "no_img_tags" in _failed_names(report)


@pytest.mark.parametrize(
    "broken,expect_keyword",
    [
        ("<!--BLOG_IMAGE:mid-->", "malformed"),          # 少了空格 → 畸形
        ("<!-- blog_image:mid -->", "malformed"),        # 小写 → 畸形
        ("", "missing placeholders"),                             # 整个删掉 → 缺槽位
    ],
)
def test_malformed_or_missing_placeholder(broken, expect_keyword):
    """占位符差一个空格就替换不上，最后渲染成一条 HTML 注释 —— 属于"看不出错的错"。"""
    blog = _good_blog()
    blog["content_html"] = blog["content_html"].replace("<!-- BLOG_IMAGE:mid -->", broken)
    report = _run(blog)
    assert _failed_names(report) == {"image_placeholders"}
    assert expect_keyword in report.failures[0]


def test_duplicated_placeholder():
    """同一个槽位写两次 → 同一张图会出现两次（历史上真踩过）。"""
    blog = _good_blog()
    blog["content_html"] += "<!-- BLOG_IMAGE:hero -->"
    report = _run(blog)
    assert _failed_names(report) == {"image_placeholders"}
    assert "hero×2" in report.failures[0]


def test_unexpected_slot_name():
    """GPT 自己发明的槽位名要被抓出来（下游没有这个槽位，图不会生成）。"""
    blog = _good_blog()
    blog["content_html"] += "<!-- BLOG_IMAGE:banner -->"
    report = _run(blog)
    assert _failed_names(report) == {"image_placeholders"}
    assert "banner" in report.failures[0]


@pytest.mark.parametrize(
    "claim",
    [
        "We offer 100% guaranteed protection for your paint.",
        "This coating will never fade or peel.",
        "The finish lasts forever with no upkeep.",
        "Our process is risk-free.",
    ],
)
def test_forbidden_absolute_claims(claim):
    """A 类硬禁词：绝对化承诺，确定性阻断，不问 LLM。"""
    blog = _good_blog()
    blog["content_html"] += f"<p>{claim}</p>"
    report = _run(blog)
    assert "forbidden_claims" in _failed_names(report)


def test_lifetime_warranty_is_allowed():
    """⚠️ 这条测的是一个**刻意的取舍**，不是普通功能。

    "lifetime warranty" 是 XPEL / Ceramic Pro 的真实条款名，贴膜行业天天出现。
    拦它会制造大量假阳性。判据：宁可漏拦有合法用法的词，也不能误拦真实业务用语。
    哪天有人想把 "lifetime" 加进硬禁表，这条测试会先红 —— 迫使他先看到这个取舍。
    """
    blog = _good_blog()
    blog["content_html"] += "<p>The installer offers a limited lifetime warranty on labor.</p>"
    report = _run(blog)
    assert report.hard_ok, report.failures


# ── 品牌禁词（租户级政策，按位置分两级）────────────────────────────────

# 一份服装电商租户的品牌禁词样例（完整表由各租户在 merchant.json 里配）
BRAND_TERMS = ("belly", "dad bod", "hide your", "flattering for", "slimming")


def _run_brand(blog: dict):
    """带品牌禁词表跑检查（其余参数照旧）。"""
    return check_seo(blog, TOPIC, website=WEBSITE, expected_slots=SLOTS, brand_terms=BRAND_TERMS)


def test_brand_term_in_title_is_hard_fail():
    """★ 真实翻车的回归测试 ★

    2026-07-27 auto 流程产出了标题
    `Best Fitting T-Shirts for Men with Belly` —— 而三个人格文件里都写了
    「不要把身体当成需要遮掩的问题」。**prompt 里的禁令不是 enforcement。**
    这条测试锁住"标题里出现禁词必须 hard 阻断"，让同样的东西再也发不出去。
    """
    blog = _good_blog(title="Best Fitting T-Shirts for Men with Belly")
    report = _run_brand(blog)
    assert not report.hard_ok, "标题里的品牌禁词必须阻断"
    assert _failed_names(report) == {"brand_voice_headline"}
    assert "belly" in report.hard_failures[0]
    assert "title" in report.hard_failures[0]          # 要指明是哪个字段
    assert "the whole angle has to change" in report.hard_failures[0]  # 指令不是"换个词"，是"换框架"


def test_brand_term_in_heading_is_also_hard():
    """小标题同样算"品牌门面"：它会进目录、进搜索结果的富摘要。"""
    blog = _good_blog()
    blog["content_html"] = blog["content_html"].replace(
        "<h2>What window tint adds to the package</h2>",
        "<h2>How to hide your problem spots</h2>",
    )
    report = _run_brand(blog)
    assert not report.hard_ok
    assert "brand_voice_headline" in _failed_names(report)
    assert "heading" in report.hard_failures[0]


def test_brand_term_in_body_is_soft_only():
    """★ 分级的关键 ★ 同一个词出现在正文里只提醒，不阻断。

    因为正文里可能有**合法用法**："the hem sits below the belly" 是中性的部位描述，
    不是"把肚子当问题"。一律 hard 会制造假阳性并触发昂贵的整篇重写；
    语境判断交给 reviewer（它的 soul 里写了 body-neutral 要求）——
    正则负责发现，LLM 负责判语境。
    """
    blog = _good_blog()
    blog["content_html"] += "<p>The hem sits just below the belly for a clean line.</p>"
    report = _run_brand(blog)
    assert report.hard_ok, "正文里的禁词是 soft，不该阻断"
    assert _failed_names(report) == {"brand_voice_body"}
    assert report.soft_failures and "belly" in report.soft_failures[0]
    # 正文那档的指令口吻不同：让人去判语境，而不是命令改写
    assert "treat the body as a problem to hide" in report.soft_failures[0]


def test_brand_terms_not_configured_means_skipped_not_passed():
    """没配词表 → 明确"跳过 + 原因"。

    这是租户隔离的体现：Harbor Auto Spa（汽车贴膜）没有 body-positive 这条政策，
    不该被服装电商的词表误伤，也不该被默默判成"通过了品牌检查"。
    """
    blog = _good_blog(title="Best Fitting T-Shirts for Men with Belly")
    report = _run(blog)                      # 不传 brand_terms
    assert report.hard_ok                    # 不拦
    voice = [c for c in report.checks if c.name.startswith("brand_voice_")]
    assert len(voice) == 2
    assert all(c.ok and "Skipped" in c.detail for c in voice)
    assert all("brand_voice.forbidden_terms" in c.detail for c in voice)  # 说清缺什么


def test_brand_term_matching_respects_word_boundaries():
    """"slimming"（禁词）不能把 "slim fit"（产品术语）误判进去。

    这是假阳性防线：服装电商的核心卖点常常就是 slim/tapered fit，
    如果宽松匹配把 "slim fit" 也拦了，这个检查第一天就会被关掉。
    """
    blog = _good_blog()
    blog["content_html"] += "<p>The slim fit tapers at the waist.</p>"
    report = _run_brand(blog)
    assert report.hard_ok, report.failures
    assert "brand_voice_body" not in _failed_names(report)


# ── 隐含假设 / 契约形状 ───────────────────────────────────────────────


def test_placeholder_comment_not_counted_as_body_text():
    """隐含假设：bs4 的 get_text() 不包含 HTML 注释内容。

    如果这个假设不成立，`<!-- BLOG_IMAGE:hero -->` 里的字符串会被算进正文，
    污染关键词统计和词数。这不是我们的代码逻辑，是依赖的行为 ——
    所以必须有测试盯着，bs4 换版本时第一时间报警。
    """
    blog = _good_blog(content_html="<p>Plain body.</p><!-- BLOG_IMAGE:hero --> WINDOWTINTMARKER")
    report = check_seo(blog, {"primary_keyword": "", "secondary_keywords": ["BLOG_IMAGE"]},
                       website=WEBSITE, expected_slots=("hero",))
    # "BLOG_IMAGE" 只存在于注释里 → 应该判定为"没出现在正文"
    assert "secondary_keywords" in _failed_names(report)


def test_missing_inputs_are_reported_as_skipped_not_passed():
    """没数据 ≠ 达标。

    铁律来自 botsrd 那个 bounce_rate=0.0 被当"跳出率 0% = 完美"的 bug：
    **缺失指标绝不能当 0 / 当通过**。这里的做法是照常返回 ok=True（不阻断出稿），
    但 detail 里明写"跳过"和原因，人一眼能看出这项没真查。
    """
    report = check_seo(_good_blog(), {}, website="", expected_slots=None)
    skipped = [c for c in report.checks if "Skipped" in c.detail]
    names = {c.name for c in skipped}
    assert names == {
        "primary_keyword_early",
        "secondary_keywords",
        "internal_links",
        "external_links",
        "image_placeholders",
        # 品牌禁词是**租户级**配置：没给词表就该明确"跳过"，
        # 而不是默认判过（Harbor Auto Spa 那边永远走这一支）
        "brand_voice_headline",
        "brand_voice_body",
        # 商品目录同理：电商租户有 catalog.json，harbor_auto_spa（服务业）没有
        "catalog_grounding",
    }
    assert all(c.ok for c in skipped)          # 不阻断
    assert all("Skipped" in c.detail for c in skipped)  # 但必须说明


def test_as_feedback_is_shape_compatible_with_reviewer_output():
    """as_feedback() 必须和 reviewer 的输出同构，才能复用同一条 rewrite 路径。"""
    report = _run(_good_blog(excerpt="too short"))
    fb = report.as_feedback()
    # rewrite_blog 读的就是这几个键（agents/copywriter.py:rewrite_blog）
    assert set(fb) >= {"score", "passed", "issues", "suggestions"}
    assert fb["passed"] is False
    assert fb["score"] == 0                 # 没评过分就是 0，不能编一个中间值
    assert fb["issues"] == report.failures
    assert fb["source"] == "deterministic"  # 溯源字段：日志里能区分是谁打回的


def test_check_seo_does_not_mutate_input():
    """校验器必须是纯函数 —— review_loop 里 blog_data 是被原地改的，
    如果校验器也动它，两边一起改同一个 dict 会出鬼。"""
    blog = _good_blog()
    import copy
    before = copy.deepcopy(blog)
    _run(blog)
    assert blog == before
