#!/usr/bin/python3
"""将唯一 Markdown 正文导出为图片内嵌的单文件教程；仅用于文档维护。"""
import argparse
import base64
import html
from pathlib import Path
import re
import unicodedata
from urllib.parse import urljoin

try:
    from markdown_it import MarkdownIt
except ImportError:
    raise SystemExit('文档导出需要 markdown-it-py；应用本身不依赖此库。')

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / 'docs'
SOURCE = DOCS / 'team-onboarding.md'
OUTPUT = DOCS / 'team-onboarding.html'
WEB = 'https://github.com/iswangyifan/openai-proxy-kit/blob/main/docs/'


def slug(text, separator):
    return ''.join(c for c in text.lower()
                   if unicodedata.category(c)[0] in 'LN' or c in ' -_').replace(' ', separator)


def render():
    body = MarkdownIt('commonmark', {'html': False}).enable('table').render(SOURCE.read_text())
    headings = {}

    def heading(match):
        title = html.unescape(re.sub(r'<[^>]+>', '', match.group(2)))
        base = slug(title, '-')
        count = headings.get(base, 0)
        headings[base] = count + 1
        anchor = base + ('-' + str(count) if count else '')
        return '<h' + match.group(1) + ' id="' + html.escape(anchor, quote=True) + '">' + match.group(2) + '</h' + match.group(1) + '>'

    body = re.sub(r'<h([1-6])>(.*?)</h\1>', heading, body, flags=re.S)
    # 在 HTML 版中去掉提示下载 HTML 的段落，避免重复引导。
    body = re.sub(r'<p><strong>看不到图片？</strong>.*?</p>\n?', '', body)

    def embedded(match):
        path = (DOCS / html.unescape(match.group(1))).resolve()
        if not path.is_relative_to(DOCS) or path.suffix.lower() != '.png':
            raise ValueError('教程插图必须是 docs 下的 PNG 文件')
        payload = base64.b64encode(path.read_bytes()).decode()
        return 'src="data:image/png;base64,' + payload + '"'

    body = re.sub(r'src="([^"]+)"', embedded, body)

    def link(match):
        target = html.unescape(match.group(1))
        if not target.startswith(('#', 'https://', 'http://', 'mailto:')):
            # 离线文件里的其他文档链接指向仓库，正文与图片本身无需联网。
            target = urljoin(WEB, target)
        return 'href="' + html.escape(target, quote=True) + '"'

    body = re.sub(r'href="([^"]+)"', link, body)
    return '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenAI Proxy Kit 新用户图文教程</title>
<style>
:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#f5f7fa;color:#202b3a;
font:16px/1.85 system-ui,-apple-system,"Noto Sans CJK SC","Microsoft YaHei",sans-serif}
main{max-width:1080px;margin:32px auto;padding:36px 44px;background:white;border:1px solid #e0e6ed;border-radius:16px}
h1{font-size:30px;line-height:1.4}h2{margin-top:52px;padding-top:20px;border-top:1px solid #e0e6ed;font-size:25px}
h3{margin-top:30px;font-size:20px}a{color:#087f9d;text-underline-offset:3px}li{margin:7px 0}
img{display:block;max-width:100%;height:auto;margin:20px auto;border:1px solid #dce5ed;border-radius:10px}
pre{padding:16px 20px;overflow:auto;background:#f1f5f9;border:1px solid #e1e7ee;border-radius:8px;line-height:1.6}
code{font-family:ui-monospace,"DejaVu Sans Mono",monospace;font-size:.9em}p code,li code{overflow-wrap:anywhere}
table{display:block;overflow:auto;border-collapse:collapse;width:100%;margin:20px 0}th,td{border:1px solid #dfe5eb;padding:12px 16px;vertical-align:top;text-align:left}th{background:#eef4f8}
.format-note{padding:12px 16px;background:#eaf5f9;border-left:4px solid #087f9d;border-radius:6px}
:target{scroll-margin-top:24px}@media(max-width:700px){main{margin:0;padding:22px 18px;border:0;border-radius:0}h1{font-size:25px}h2{font-size:22px}}
@media print{body{background:white}main{margin:0;max-width:none;padding:0;border:0}img,pre,tr{break-inside:avoid}h2,h3{break-after:avoid}}
</style></head><body><main>
<div class="format-note">完整图文离线版 · 插图已内嵌，可保存此文件后用浏览器打开。<a href="''' + WEB + '''team-onboarding.md">GitHub 文档</a></div>
''' + body + '\n</main></body></html>\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='检查已生成 HTML 是否与正文和插图一致')
    args = parser.parse_args()
    result = render()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text() != result:
            raise SystemExit('HTML 需要更新，请运行 packaging/render-user-guide.py')
        print('PASS: HTML 与 Markdown、内嵌插图一致')
    else:
        OUTPUT.write_text(result)
        print('已生成 docs/team-onboarding.html（全部插图内嵌）')
