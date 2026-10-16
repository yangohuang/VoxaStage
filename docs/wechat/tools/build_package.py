"""Build offline reader HTML and a WeChat editorial handoff ZIP; does not publish."""
from pathlib import Path
from html import escape
from html.parser import HTMLParser
import hashlib
import json
import re
import shutil
import zipfile
import markdown

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'build'
STYLE = {
 'h1':'font-size:27px;line-height:1.45;color:#163044;margin:24px 0;',
 'h2':'font-size:21px;line-height:1.5;color:#007e87;margin:32px 0 16px;',
 'h3':'font-size:18px;color:#163044;margin:26px 0 12px;',
 'p':'font-size:17px;line-height:1.95;margin:16px 0;color:#273d4b;overflow-wrap:anywhere;',
 'li':'font-size:16px;line-height:1.9;margin:8px 0;overflow-wrap:anywhere;',
 'ul':'padding-left:22px;', 'ol':'padding-left:24px;',
 'img':'display:block;width:100%;height:auto;margin:22px auto;border-radius:8px;',
 'a':'color:#007e87;text-decoration:underline;overflow-wrap:anywhere;',
 'blockquote':'border-left:3px solid #007e87;padding-left:16px;margin:20px 0;',
 'pre':'font-size:13px;white-space:pre-wrap;overflow-wrap:anywhere;background:#eff4f7;padding:14px;border-radius:8px;',
 'code':'font-size:0.88em;font-family:monospace;',
 'table':'width:100%;border-collapse:collapse;font-size:14px;table-layout:fixed;',
 'th':'border:1px solid #d8e3e9;padding:8px;overflow-wrap:anywhere;',
 'td':'border:1px solid #d8e3e9;padding:8px;overflow-wrap:anywhere;',
}

def destination(source, link):
    if '://' in link or link.startswith(('#','mailto:')): return link
    target=(source.parent/link.split('#')[0]).resolve()
    if not target.is_relative_to(ROOT):
        relative=target.relative_to(ROOT.parents[1])
        return 'https://github.com/yangohuang/VoxaStage/blob/dd44878/'+relative.as_posix()+('#'+link.split('#',1)[1] if '#' in link else '')
    relative=target.relative_to(ROOT)
    if target.suffix=='.md':
        name='index.html' if relative==Path('README.md') else relative.as_posix().replace('/','--')[:-3]+'.html'
        return name + ('#'+link.split('#',1)[1] if '#' in link else '')
    return relative.as_posix()

class Inline(HTMLParser):
    def __init__(self,source): super().__init__(convert_charrefs=False);self.source=source;self.parts=[]
    def handle_starttag(self,tag,attrs):
        values=dict(attrs)
        if tag in STYLE:values['style']=STYLE[tag]
        for key in ['href','src']:
            if key in values:values[key]=destination(self.source,values[key])
        self.parts.append('<'+tag+''.join(f' {k}="{escape(v or "",quote=True)}"' for k,v in values.items())+'>')
    def handle_endtag(self,tag):self.parts.append(f'</{tag}>')
    def handle_data(self,data):self.parts.append(data)
    def handle_entityref(self,name):self.parts.append('&'+name+';')
    def handle_charref(self,name):self.parts.append('&#'+name+';')

def html_page(title,content):
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+escape(title)+'</title><style>html{background:#edf2f6}body{margin:0}main{box-sizing:border-box;max-width:740px;margin:auto;padding:28px 22px 60px;background:white}img{max-width:100%}*{box-sizing:border-box}</style></head><body><main>'+content+'</main></body></html>'

def main():
    if OUT.exists():shutil.rmtree(OUT)
    OUT.mkdir()
    series=json.loads((ROOT/'series.json').read_text())
    for source in sorted(ROOT.rglob('*.md')):
        if OUT in source.parents:continue
        text=source.read_text()
        title=text.splitlines()[0].lstrip('# ')
        if '<!-- article:begin -->' in text:
            header,remaining=text.split('<!-- article:begin -->',1)
            body,refs=remaining.split('<!-- article:end -->',1)
            num=source.name.split('-')[0]
            prerequisite=next(line.lstrip('- ') for line in header.splitlines() if line.lstrip('- ').startswith('阅读前置知识：'))
            text=f'# {title}\n\n![第{num}篇封面](../assets/{num}-cover.png)\n\n{prerequisite}\n\n'+body+refs
        renderer=Inline(source);renderer.feed(markdown.markdown(text,extensions=['tables','fenced_code']))
        name=destination(source,source.name)
        (OUT/name).write_text(html_page(title,''.join(renderer.parts)))
    # The landing page uses cards, avoiding wide editorial tables on phones.
    entries=[]
    for item in series['articles']:
        url=destination(ROOT/'README.md',item['file'])
        label='完整稿' if item['status']=='draft' else '选题简报'
        entries.append(f'<p style="{STYLE["p"]}"><a style="{STYLE["a"]}" href="{url}">{escape(item["id"]+" · "+item["title"])}</a><br><small>{label} · 尚未在公众号发表</small></p>')
    landing=f'<h1 style="{STYLE["h1"]}">{escape(series["series"])}</h1><p style="{STYLE["p"]}">前三篇完整稿与素材，后续选题简报。此页为离线阅读入口，不是公众号页面。</p>'+''.join(entries)+f'<p style="{STYLE["p"]}"><a href="PUBLISHING.html">发布说明</a> · <a href="DELIVERY.html">交付检查</a> · <a href="EVIDENCE.html">证据索引</a></p>'
    (OUT/'index.html').write_text(html_page(series['series'],landing))
    shutil.copytree(ROOT/'assets',OUT/'assets')
    shutil.copy2(ROOT.parents[1]/'LICENSE',OUT/'LICENSE')
    for source in ROOT.glob('*.json'):shutil.copy2(source,OUT/source.name)
    sources=[p for p in ROOT.rglob('*') if p.is_file() and OUT not in p.parents and '__pycache__' not in p.parts and p.suffix in {'.md','.json','.py','.png','.svg'}]
    for source in sources:
        target=OUT/'source'/source.relative_to(ROOT);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    manifest=[{'path':p.relative_to(OUT).as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(OUT.rglob('*')) if p.is_file()]
    (OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    archive=OUT/'voxastage-wechat-batch-01.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(OUT.rglob('*')):
            if p.is_file() and p!=archive:
                info=zipfile.ZipInfo(p.relative_to(OUT).as_posix(),date_time=(2026,10,16,0,0,0))
                info.compress_type=zipfile.ZIP_DEFLATED
                info.external_attr=0o644 << 16
                z.writestr(info,p.read_bytes())
    print(json.dumps({'reader':'docs/wechat/build/index.html','archive':'docs/wechat/build/'+archive.name,'files':len(manifest)+1,'zip_sha256':hashlib.sha256(archive.read_bytes()).hexdigest()},ensure_ascii=False))
if __name__=='__main__':main()
