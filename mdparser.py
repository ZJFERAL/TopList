"""Markdown 解析：frontmatter 元数据 + 任务行（- [ ] / - [x]），其余行视为普通文本。"""
import re

import yaml

TASK_RE = re.compile(r'^(?P<indent>\s*)- \[(?P<mark>[ xX])\] (?P<text>.*)$')
HEADING_RE = re.compile(r'^(?P<level>#{1,6})\s+(?P<text>.*?)\s*$')
FRONTMATTER_RE = re.compile(r'\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)', re.DOTALL)


def parse_markdown(text: str) -> dict:
    """返回 {'frontmatter': dict, 'groups': [{'title': str|None, 'items': [...]}]}。

    item 两种：
      task:  {'type': 'task', 'line': 全文行号, 'indent': 缩进列数, 'checked': bool, 'text': str}
      prose: {'type': 'prose', 'text': str}
    """
    frontmatter = {}
    body_offset = 0
    m = FRONTMATTER_RE.match(text)
    if m:
        body_offset = m.end()
        try:
            data = yaml.safe_load(m.group(1))
            if isinstance(data, dict):
                frontmatter = data
        except yaml.YAMLError:
            frontmatter = {'_raw': m.group(1).strip()}

    body = text[body_offset:]
    # 行号必须是全文行号，勾选写回时才能定位到正确的行
    line_base = text[:body_offset].count('\n')

    groups = [{'title': None, 'items': []}]
    for i, raw in enumerate(body.split('\n')):
        no = line_base + i
        h = HEADING_RE.match(raw)
        if h:
            groups.append({'title': h.group('text'), 'items': []})
            continue
        t = TASK_RE.match(raw.rstrip())
        if t:
            groups[-1]['items'].append({
                'type': 'task',
                'line': no,
                'indent': len(t.group('indent').expandtabs(4)),
                'checked': t.group('mark') != ' ',
                'text': t.group('text').strip(),
            })
        elif raw.strip():
            groups[-1]['items'].append({'type': 'prose', 'text': raw.rstrip()})

    return {
        'frontmatter': frontmatter,
        'groups': [g for g in groups if g['title'] or g['items']],
    }
