"""Render original editorial diagrams as editable SVG and matching PNG (Pillow)."""
from pathlib import Path
from html import escape
import argparse
import subprocess
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
INK, MUTED, BG, ACCENT = '#163044', '#476579', '#f4f7fa', '#007e87'

class Canvas:
    def __init__(self, width, height, font):
        self.w, self.h, self.font = width, height, font
        self.image = Image.new('RGB', (width, height), BG)
        self.draw = ImageDraw.Draw(self.image)
        self.svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">', f'<rect width="{width}" height="{height}" fill="{BG}"/>']
    def rect(self, x,y,w,h,fill,stroke=None):
        self.draw.rounded_rectangle((x,y,x+w,y+h),radius=20,fill=fill,outline=stroke,width=2)
        self.svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="20" fill="{fill}" stroke="{stroke or fill}" stroke-width="2"/>')
    def text(self,x,y,value,size=38,fill=INK):
        font=ImageFont.truetype(self.font,size)
        if self.draw.textlength(value,font=font)>self.w-x-40:
            raise ValueError(f'Text exceeds canvas: {value}')
        self.draw.text((x,y),value,font=font,fill=fill,anchor='lt')
        self.svg.append(f'<text x="{x}" y="{y}" font-family="Noto Sans CJK SC, sans-serif" font-size="{size}" dominant-baseline="text-before-edge" fill="{fill}">{escape(value)}</text>')
    def arrow(self,x,y):
        self.draw.line((x,y,x,y+32),fill=ACCENT,width=4)
        self.draw.polygon([(x-10,y+23),(x+10,y+23),(x,y+38)],fill=ACCENT)
        self.svg.append(f'<path d="M{x} {y}v32m-10 -9l10 15 10 -15" stroke="{ACCENT}" stroke-width="4" fill="none"/>')
    def save(self,name):
        directory=ROOT/'assets';directory.mkdir(exist_ok=True)
        self.image.save(directory/f'{name}.png')
        (directory/f'{name}.svg').write_text('\n'.join(self.svg+['</svg>'])+'\n')

def diagram(name,title,subtitle,rows,footer,font,arrows=True):
    height=240+len(rows)*238+80
    c=Canvas(1080,height,font)
    c.text(60,42,'VOXASTAGE  /  实时数字人，从模型到交互',25,ACCENT)
    c.text(60,100,title,46)
    c.text(60,168,subtitle,30,MUTED)
    for i,(label,lines) in enumerate(rows):
        y=235+i*238
        c.rect(55,y,970,195,'#ffffff','#d9e4eb')
        c.rect(55,y,8,195,ACCENT)
        c.text(88,y+24,label,38,ACCENT)
        for j,line in enumerate(lines):c.text(88,y+83+j*48,line,36)
        if i<len(rows)-1 and arrows:c.arrow(540,y+200)
    c.text(60,height-55,footer,27,MUTED)
    c.save(name)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font',help='CJK font file; otherwise resolved through fontconfig')
    args=parser.parse_args()
    font=args.font or subprocess.check_output(['fc-match','-f','%{file}','Noto Sans CJK SC'],text=True).strip()
    covers=[('01','让数字人真正进入对话','语音 · Agent · 形象，三层怎么协作'),('02','两条语音链路，一套系统','级联与端到端：接口、调试与边界'),('03','“停一下”之后，发生什么？','任务取消 · 旧结果隔离 · 播放确认')]
    for num,title,subtitle in covers:
        c=Canvas(1080,460,font)
        c.rect(0,0,1080,460,'#102e42')
        c.rect(52,50,95,60,'#0a8f97')
        c.text(74,60,num,42,'#ffffff')
        c.text(175,67,'实时数字人，从模型到交互',32,'#a4d9dc')
        c.text(52,173,title,64,'#ffffff')
        c.text(52,280,subtitle,36,'#d2e3ec')
        c.text(52,386,'VoxaStage  /  工程实践系列',27,'#a4d9dc')
        c.save(f'{num}-cover')
    diagram('01-layers','一个数字人的三层职责','职责示意；编排层协调另外两层',[
        ('01 / 对话后端',['ASR → LLM → TTS / MiniCPM-o','理解输入，组织回答，产生声音']),
        ('02 / 会话与 Agent 编排',['工具执行、话轮切换、上下文','判断任务是否仍属于有效话轮']),
        ('03 / 形象与浏览器播放',['DINet / FlashHead / StreamingTalker','视频或网格跟随音频播放时钟'])], '模型能力来自上游；适配、生命周期与验证来自本项目。',font,False)
    diagram('01-lifecycle','用户改口，系统如何收尾？','交互机制示意，不代表实测耗时',[
        ('01 / 停止旧播放',['停止音源，清理排队媒体','让当前可见、可听输出停止']),
        ('02 / 取消旧任务',['旧话轮失效，尝试取消计算','取消信号不等于计算立即结束']),
        ('03 / 拒绝迟到结果',['检查任务与话轮的有效性','旧工具、音频和画面不能接管新轮']),
        ('04 / 修正模型历史',['保留确认播完的完整语句段','未确认内容记录为中断'])], '上下文依据段级播放确认；不等于逐词或声学确认。',font)
    diagram('02-paths','两条语音路径，共用会话层','模型链路不同；当前都由话轮边界管理',[
        ('级联路径',['VAD 分段 → ASR → LLM → TTS','中间文本可检查；接入受限工具执行']),
        ('端到端路径',['语音 / 按需图片 → MiniCPM-o','生成语音；尚未接入同套工具调度']),
        ('共同输出约束',['选择形象 → 配对音画 → 浏览器','共享取消、迟到过滤与播放生命周期'])], '端到端 ≠ 流式 ≠ 原生全双工；速度需统一边界测量。',font,False)
    diagram('02-tradeoffs','比较模型前，先统一观察点','没有脱离实现条件的延迟胜负',[
        ('级联：逐环节定位',['转写文本 → 模型消息 → 合成音频','中间结果明确，也增加跨服务边界']),
        ('端到端：检查输入与整体输出',['语音上下文 → 模型 → 输出语音','减少显式模块，内部错误归因更难']),
        ('共同：比较真实用户体验',['质量、首声、打断、恢复与资源','模型首字不能代替物理首声'])], '同硬件、同输入、同测量口径，才能解释差异。',font,False)
    diagram('03-cancellation','一次打断，四处都要响应','示意顺序；具体操作可异步发生',[
        ('播放端',['停止旧音源，清空音画队列','让旧帧不能再次显示']),
        ('任务端',['使旧话轮失效并发起取消','避免把协程取消当成硬件停算']),
        ('结果入口',['工具与媒体返回时检查归属','拒绝已经失效的话轮结果']),
        ('历史上下文',['核对完整片段的播放确认','保留已播部分，标记中断'])], '这里描述有效打断后的处理；不宣称已解决语义打断。',font)
    diagram('03-playback','生成过，不等于用户听到过','以三个语句段 A / B / C 为例',[
        ('A / 已自然播完并确认',['模型历史保留完整语句段','服务端核对已发送的采样数量']),
        ('B / 播放中途被打断',['不能把整段当作已经听完','记录中断；不推断逐词进度']),
        ('C / 已生成但尚未播放',['不能当作已向用户解释过','显示记录与模型历史分开处理'])], '播放确认是浏览器事件，不证明声音实际被人听见。',font,False)
    print('Rendered 3 covers and 6 diagrams as PNG + SVG')
if __name__=='__main__':main()
