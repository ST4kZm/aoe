"""Render existing maintenance graph and schedule without changing either input."""
import argparse
import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault('MPLCONFIGDIR', str(Path(tempfile.gettempdir()) / 'maintenance-mpl'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Patch
from matplotlib.lines import Line2D

from scripts.maintenance.pipeline import topological_order


def save(fig, output, name):
    for extension in ('png', 'svg'):
        fig.savefig(output / f'{name}.{extension}', dpi=180, facecolor='white')
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph', required=True)
    parser.add_argument('--schedule', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    graph = json.loads(Path(args.graph).read_text())
    schedule = json.loads(Path(args.schedule).read_text())
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    fonts = {f.name for f in font_manager.fontManager.ttflist}
    plt.rcParams['font.family'] = next((f for f in ['Arial Unicode MS', 'Noto Sans CJK SC', 'Heiti TC'] if f in fonts), 'sans-serif')
    plt.rcParams['axes.unicode_minus'] = False
    tasks = {t['id']: t for t in schedule['tasks']}
    unresolved = graph.get('unresolved_pairs', [])
    if graph.get('schema_version') == 2:
        graph_note = f'条件性结果：{len(unresolved)} 对物理关系尚未解决；资源可行不代表工艺逻辑已经验证。'
        overlap_note = f"未知关系中有 {len(schedule.get('unresolved_overlaps', []))} 对在候选排程中重叠；未知资源顺序仅为假设。"
    else:
        graph_note = '历史三分类结果：VAGUE 未区分独立与未知；资源可行不代表工艺逻辑已经验证。'
        overlap_note = '历史三分类排程，仅展示原始结果；未重新评估工艺关系。'
    edges = graph['edges']
    if {tuple(e) for e in edges} != {tuple(e) for e in schedule['edges']}:
        raise ValueError('Graph and schedule have different edges')
    order = topological_order(list(tasks), edges)
    depth = {n: 0 for n in order}
    for n in order:
        for a, b in edges:
            if a == n:
                depth[b] = max(depth[b], depth[a] + 1)
    layers = {}
    for n in order:
        layers.setdefault(depth[n], []).append(n)
    positions = {}
    for level, nodes in layers.items():
        nodes.sort()
        for i, n in enumerate(nodes):
            positions[n] = (level * 2.2, (i - (len(nodes)-1)/2) * 2)
    teal, orange, red = '#287e91', '#ca791c', '#b33f46'
    fig, ax = plt.subplots(figsize=(22, 8))
    records = {(e['source'], e['target']): e['types'] for e in graph['edge_records']}
    for a, b in edges:
        types = records[a, b]
        color = orange if types == ['resource'] else teal if types == ['physical'] else '#7856a0'
        x1, y1 = positions[a]; x2, y2 = positions[b]
        ax.add_patch(FancyArrowPatch((x1+.86, y1), (x2-.86, y2),
                     arrowstyle='-|>', mutation_scale=13, linewidth=1.6,
                     linestyle='--' if types == ['resource'] else '-', color=color,
                     connectionstyle='arc3,rad=0.05', zorder=1))
    for n, (x, y) in positions.items():
        t = tasks[n]
        critical = t['total_float'] == 0
        crane = bool(t['resources'].get('crane'))
        ax.add_patch(FancyBboxPatch((x-.84, y-.48), 1.68, .96,
                     boxstyle='round,pad=0.03', facecolor='#fff1dc' if crane else '#ecf5f7',
                     edgecolor=red if critical else '#8da5ad', linewidth=2 if critical else 1, zorder=2))
        ax.text(x, y, f"{n}  {t['duration']}天\n{t['name']}\n{t['start']}–{t['end']}天", ha='center', va='center', fontsize=9, zorder=3)
    ax.set_xlim(-1.2, max(depth.values())*2.2+1.2)
    ax.set_ylim(-3.6, 3.6)
    ax.axis('off')
    ax.set_title(f"实际生成的依赖图  |  {len(tasks)} 项工序 · {len(edges)} 条边 · CPM {schedule['makespan']} 天\n节点按依赖层级排列，横向距离不表示时间", loc='left', fontsize=17, pad=20)
    ax.legend(handles=[Line2D([0],[0],color=teal,label='物理依赖'),
                       Line2D([0],[0],color=orange,linestyle='--',label='资源顺序'),
                       Line2D([0],[0],color='#7856a0',label='物理 + 资源'),
                       Patch(facecolor='white',edgecolor=red,linewidth=2,label='关键工序'),
                       Patch(facecolor='#fff1dc',label='占用吊车')],
              loc='lower center', ncol=5, frameon=False)
    fig.text(.04,.035,graph_note,fontsize=12,color=red)
    fig.subplots_adjust(left=.025,right=.98,top=.82,bottom=.12)
    save(fig,output,'dependencies')

    rows = sorted(tasks.values(), key=lambda t: t['id'])
    fig, (ax, crane_ax) = plt.subplots(2,1,figsize=(15,11),sharex=True,gridspec_kw={'height_ratios':[17,2]})
    for i,t in enumerate(rows):
        color = orange if t['resources'].get('crane') else teal
        ax.barh(i,t['duration'],left=t['start'],height=.65,color=color,
                edgecolor=red if t['total_float']==0 else color,linewidth=2)
        if t['total_float']:
            ax.barh(i,t['total_float'],left=t['end'],height=.4,facecolor='none',edgecolor='#adb7c1',hatch='///')
        ax.text((t['start']+t['end'])/2,i,f"{t['start']}–{t['end']}",ha='center',va='center',color='white',fontsize=10)
    ax.set_yticks(range(len(rows)),[f"{t['id']}  {t['name']}" for t in rows])
    ax.invert_yaxis()
    ax.set_title(f"实际排程甘特图  |  总工期 {schedule['makespan']} 天\n串行 {schedule['serial_duration']} 天 · 当前图下缩短 {schedule['reduction_percent']}%",loc='left',fontsize=18,pad=24)
    ax.legend(handles=[Patch(color=teal,label='普通工序'),Patch(color=orange,label='占用吊车'),
                       Patch(facecolor='white',edgecolor=red,linewidth=2,label='关键工序'),
                       Patch(facecolor='white',edgecolor='#adb7c1',hatch='///',label='总时差（示意）')],loc='lower right',ncol=4,bbox_to_anchor=(1,1.01),frameon=False)
    for t in sorted(rows,key=lambda t:t['start']):
        if t['resources'].get('crane'):
            crane_ax.barh(0,t['duration'],left=t['start'],height=.55,color=orange,edgecolor='white')
            crane_ax.text((t['start']+t['end'])/2,0,t['id'],ha='center',va='center',color='white',fontsize=9)
    crane_ax.set_yticks([0],['吊车（1 台）'])
    crane_ax.set_xlabel('距项目开始的时间 / 天（从 0 开始，工序占用区间为 [开始, 完成)）',fontsize=11)
    for axes in (ax,crane_ax):
        axes.set_xlim(0,schedule['makespan']+.25)
        axes.set_xticks(range(schedule['makespan']+1))
        axes.grid(axis='x',alpha=.18)
        axes.set_axisbelow(True)
        axes.spines[['top','right']].set_visible(False)
    fig.text(.15,.035,overlap_note + '\n图中忠实展示原始结果；未补边、未重新优化。总时差不能视为各工序可同时独立使用的余量。',fontsize=11,color=red)
    fig.subplots_adjust(left=.16,right=.97,top=.85,bottom=.13,hspace=.12)
    save(fig,output,'gantt')
    print(f'Generated dependencies.png/svg and gantt.png/svg in {output}')


if __name__ == '__main__':
    main()
