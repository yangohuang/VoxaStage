"""Validate editorial scope, article lengths, links, images, privacy and ZIP hashes."""
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import unquote
import hashlib
import json
import re
import zipfile
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
REPO=ROOT.parents[1]

def body_count(text):
    body=text.split('<!-- article:begin -->')[1].split('<!-- article:end -->')[0]
    body=re.sub(r'```[\s\S]*?```','',body)
    body=re.sub(r'!\[[^\]]*\]\([^)]+\)','',body)
    body=re.sub(r'^\*图.*\*$','',body,flags=re.M)
    return len(re.findall(r'[\u4e00-\u9fff]',body))

class Links(HTMLParser):
    def __init__(self):super().__init__();self.links=[];self.scripts=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='script':self.scripts.append(attrs)
        for k in ('href','src'):
            if k in attrs:self.links.append(attrs[k])

def main():
    series=json.loads((ROOT/'series.json').read_text());assert len(series['articles'])==15
    assert not series['publication_started']
    counts={};links=0
    patterns=[r'we[ _-]*'+'bank|微'+'众',r'/home/'+'yg\b|/data/'+'cfs\b|/data/'+'userdata\b',r'gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{25,}',r'(?<![\w.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2})(?![\w.])']
    for p in ROOT.rglob('*'):
        if not p.is_file() or 'build' in p.relative_to(ROOT).parts or '__pycache__' in p.parts:continue
        if p.suffix in {'.md','.json','.svg','.py'}:
            text=p.read_text()
            assert not any(re.search(pattern,text,re.I) for pattern in patterns),f'privacy match: {p.name}'
            if p.suffix in {'.md','.svg'}:
                assert not re.search(r'\bco[d]ex\b|\bchatg[p]t\b|\bg[p]t(?:-\w+)?\b',text,re.I),f'branding: {p.name}'
            if p.suffix=='.md':
                for link in re.findall(r'!?\[[^\]]*\]\(([^)]+)\)',text):
                    if link.startswith('https://github.com/yangohuang/VoxaStage/blob/dd44878/'):
                        relative=link.split('/blob/dd44878/',1)[1].split('#')[0];assert (REPO/relative).is_file(),(p.name,relative)
                    elif '://' not in link and not link.startswith('#'):
                        assert (p.parent/unquote(link.split('#')[0])).exists(),(p.name,link)
                    links+=1
        if p.suffix=='.png':
            with Image.open(p) as image:image.verify()
            assert p.with_suffix('.svg').is_file()
    for item in series['articles']:
        text=(ROOT/item['file']).read_text();assert item['wechat_url'] is None and item['published_at'] is None
        if item['status']!='draft':
            assert '选题简报' in text and '发布门槛' in text
            continue
        n=body_count(text);lo,hi=item['body_han_range'];assert lo<=n<=hi,(item['id'],n,lo,hi);counts[item['id']]=n
        body=text.split('<!-- article:begin -->')[1].split('<!-- article:end -->')[0]
        assert 2<=len(re.findall(r'!\[',body))<=4
        blocks=re.findall(r'```[^\n]*\n([\s\S]*?)```',body);assert len(blocks)<=2 and all(len(b.splitlines())<=25 for b in blocks)
        assert '摘要：' in text and '阅读前置知识：' in text and '封面主文案：' in text
    build=ROOT/'build';manifest=json.loads((build/'manifest.json').read_text())
    source_paths={p.relative_to(ROOT) for p in ROOT.rglob('*') if p.is_file() and build not in p.parents and '__pycache__' not in p.parts and p.suffix in {'.md','.json','.py','.png','.svg'}}
    packaged_paths={p.relative_to(build/'source') for p in (build/'source').rglob('*') if p.is_file()}
    assert source_paths==packaged_paths,'Source file set changed; rebuild the package'
    for path in source_paths:
        assert (ROOT/path).read_bytes()==(build/'source'/path).read_bytes(),f'Stale source snapshot: {path}'
    for item in manifest:assert hashlib.sha256((build/item['path']).read_bytes()).hexdigest()==item['sha256']
    for page in build.glob('*.html'):
        parser=Links();parser.feed(page.read_text());assert not parser.scripts
        for link in parser.links:
            if '://' not in link and not link.startswith(('#','mailto:')):assert (page.parent/unquote(link.split('#')[0])).exists(),(page.name,link)
    for item in series['articles']:
        if item['status']!='draft':continue
        html=build/(item['file'].replace('/','--')[:-3]+'.html')
        link='source/'+item['file']
        assert f'href="{link}"' in html.read_text() and f'href="{link}"' in (build/'index.html').read_text()
        assert (build/link).read_bytes()==(ROOT/item['file']).read_bytes()
    with zipfile.ZipFile(build/'voxastage-wechat-batch-01.zip') as archive:
        assert archive.testzip() is None
        assert set(archive.namelist())=={item['path'] for item in manifest}|{'manifest.json'}
        for item in manifest:assert hashlib.sha256(archive.read(item['path'])).hexdigest()==item['sha256']
    result={'status':'passed','body_han_characters':counts,'source_links_checked':links,'png_assets':len(list((ROOT/'assets').glob('*.png'))),'svg_sources':len(list((ROOT/'assets').glob('*.svg'))),'package_files':len(manifest)+1,'publication':'not uploaded or published','scope':'content structure, local source links, privacy patterns and artifact consistency; not platform rendering or live model quality'}
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
