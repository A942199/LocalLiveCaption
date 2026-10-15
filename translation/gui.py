"""Tkinter translation settings and full history management; ASR-independent."""
import json
from dataclasses import asdict
from pathlib import Path

from .api import ENGINES, TranslationSettings
from .history import HistoryStore

ROOT=Path(__file__).resolve().parent.parent
FIELDS=("api_url","api_key","app_key","app_secret","app_id",
        "model_name","temperature","source_language","keep_alive")
PASSWORD={"api_key","app_secret"}
LANGUAGES=("zh-CN","zh-TW","en-US","en-GB","ja-JP","ko-KR","fr-FR",
           "th-TH","ru-RU","es-ES","pt-BR","tr-TR","ar-SA")


def normalize_profile(values):
    """GUI variables are strings; JSON protocol requires numeric values."""
    result={key:value.strip() for key,value in values.items() if value.strip()}
    if "temperature" in result:
        result["temperature"]=float(result["temperature"])
    if "keep_alive" in result:
        result["keep_alive"]=int(result["keep_alive"])
    return result


def open_settings(parent,config_path=None):
    """Multi-profile engine editor mirroring Configs + ConfigIndices."""
    import tkinter as tk
    from tkinter import ttk,messagebox
    path=Path(config_path or ROOT/"translation-settings.json")
    settings=TranslationSettings.from_file(path)
    win=tk.Toplevel(parent)
    win.title("翻译设置 | LiveCaptions-Translator Python")
    win.attributes("-topmost",True)
    win.resizable(False,False)
    body=ttk.Frame(win,padding=14)
    body.pack(fill="both",expand=True)
    engine=tk.StringVar(value=settings.engine)
    target=tk.StringVar(value=settings.target)
    context=tk.BooleanVar(value=settings.context_aware)
    log_only=tk.BooleanVar(value=settings.log_only)
    num_contexts=tk.IntVar(value=settings.num_contexts)
    show_history=tk.IntVar(value=settings.display_sentences)
    prompt=tk.StringVar(value=settings.prompt)
    vars_={key:tk.StringVar() for key in FIELDS}
    profiles={}
    for name in ENGINES:
        raw=settings.configs.get(name,{})
        if isinstance(raw,list):
            profiles[name]=[dict(v) for v in raw] or [{}]
        else:
            profiles[name]=[dict(raw or {})]
    indices={name:max(0,min(int(settings.config_indices.get(name,0)),
                             len(profiles[name])-1)) for name in ENGINES}
    selected=[settings.engine,indices[settings.engine]]
    profile_index=tk.StringVar()

    def display():
        name,idx=selected
        profile_box["values"]=[str(i+1) for i in range(len(profiles[name]))]
        profile_index.set(str(idx+1))
        cfg=profiles[name][idx]
        for key in FIELDS:
            camel="".join(word.title() for word in key.split("_"))
            vars_[key].set(str(cfg.get(key,cfg.get(camel,""))))

    def remember():
        name,idx=selected
        profiles[name][idx]=normalize_profile({k:v.get() for k,v in vars_.items()})

    def switch_engine(event=None):
        try:
            remember()
        except ValueError as error:
            messagebox.showerror("设置无效",str(error),parent=win)
            engine.set(selected[0])
            return
        name=engine.get()
        selected[:]=[name,indices[name]]
        display()

    def switch_profile(event=None):
        try:
            remember()
        except ValueError as error:
            messagebox.showerror("设置无效",str(error),parent=win)
            profile_index.set(str(selected[1]+1))
            return
        idx=int(profile_index.get())-1
        indices[selected[0]]=idx
        selected[1]=idx
        display()

    def add_profile():
        remember()
        name=selected[0]
        profiles[name].append(dict(profiles[name][selected[1]]))
        selected[1]=len(profiles[name])-1
        indices[name]=selected[1]
        display()

    def delete_profile():
        name=selected[0]
        if len(profiles[name])<=1:
            messagebox.showinfo("至少保留一份","每个引擎至少需要一份配置。",parent=win)
            return
        del profiles[name][selected[1]]
        selected[1]=min(selected[1],len(profiles[name])-1)
        indices[name]=selected[1]
        display()

    ttk.Label(body,text="翻译引擎").grid(row=0,column=0,sticky="w")
    box=ttk.Combobox(body,textvariable=engine,values=ENGINES,state="readonly",width=28)
    box.grid(row=0,column=1,sticky="ew",padx=8,pady=3)
    box.bind("<<ComboboxSelected>>",switch_engine)
    ttk.Label(body,text="配置档案").grid(row=1,column=0,sticky="w")
    profile_row=ttk.Frame(body)
    profile_row.grid(row=1,column=1,sticky="w",padx=8,pady=3)
    profile_box=ttk.Combobox(profile_row,textvariable=profile_index,state="readonly",width=8)
    profile_box.pack(side="left")
    profile_box.bind("<<ComboboxSelected>>",switch_profile)
    ttk.Button(profile_row,text="复制新增",command=add_profile).pack(side="left",padx=6)
    ttk.Button(profile_row,text="删除当前",command=delete_profile).pack(side="left")
    ttk.Label(body,text="目标语言").grid(row=2,column=0,sticky="w")
    ttk.Combobox(body,textvariable=target,values=LANGUAGES,width=28).grid(
        row=2,column=1,padx=8,pady=3)
    ttk.Checkbutton(body,text="启用翻译上下文",variable=context).grid(
        row=3,column=0,columnspan=2,sticky="w")
    ttk.Checkbutton(body,text="仅记录模式（暂停翻译）",variable=log_only).grid(
        row=4,column=0,columnspan=2,sticky="w")
    ttk.Label(body,text="上下文条数").grid(row=5,column=0,sticky="w")
    tk.Spinbox(body,from_=0,to=10,textvariable=num_contexts,width=8).grid(
        row=5,column=1,sticky="w",padx=8)
    ttk.Label(body,text="历史显示句数").grid(row=6,column=0,sticky="w")
    tk.Spinbox(body,from_=0,to=10,textvariable=show_history,width=8).grid(
        row=6,column=1,sticky="w",padx=8)
    ttk.Label(body,text="LLM Prompt").grid(row=7,column=0,sticky="w")
    ttk.Entry(body,textvariable=prompt,width=41).grid(row=7,column=1,padx=8,pady=4)
    ttk.Separator(body).grid(row=8,column=0,columnspan=2,sticky="ew",pady=5)
    for i,key in enumerate(FIELDS,9):
        ttk.Label(body,text=key).grid(row=i,column=0,sticky="w")
        ttk.Entry(body,textvariable=vars_[key],width=41,
                  show="*" if key in PASSWORD else "").grid(row=i,column=1,padx=8,pady=3)
    display()
    ttk.Label(body,text="保存后即时生效；API 密钥会保存在本机 JSON 中。",
              foreground="#666666").grid(row=18,column=0,columnspan=2,sticky="w",pady=5)

    def save():
        try:
            remember()
            settings.engine=engine.get()
            settings.target=target.get().strip() or "zh-CN"
            settings.context_aware=bool(context.get())
            settings.log_only=bool(log_only.get())
            settings.num_contexts=max(0,min(10,int(num_contexts.get())))
            settings.display_sentences=max(0,min(10,int(show_history.get())))
            settings.prompt=prompt.get()
            settings.configs=profiles
            settings.config_indices=indices
            content=json.dumps(asdict(settings),ensure_ascii=False,indent=2)
            path.parent.mkdir(parents=True,exist_ok=True)
            tmp=path.with_suffix(".json.tmp")
            tmp.write_text(content+"\n",encoding="utf-8")
            tmp.replace(path)
            # Active translation sinks observe the new config immediately.
            from .engine import ACTIVE_SINKS
            for sink in tuple(ACTIVE_SINKS):
                sink.update_settings(settings)
        except (ValueError,OSError,TypeError) as exc:
            messagebox.showerror("保存失败",str(exc),parent=win)
            return
        win.destroy()
    ttk.Button(body,text="保存",command=save).grid(row=19,column=0,sticky="e",pady=5)
    ttk.Button(body,text="取消",command=win.destroy).grid(row=19,column=1,sticky="e",pady=5)
    return win


def open_history(parent,history_path=None):
    """Full upstream history features: search, pagination, clear, export all."""
    import tkinter as tk
    from tkinter import ttk,filedialog,messagebox
    store=HistoryStore(history_path or ROOT/"translation-history.sqlite3")
    win=tk.Toplevel(parent)
    win.title("翻译历史")
    win.geometry("1000x500")
    win.attributes("-topmost",True)
    controls=ttk.Frame(win,padding=8)
    controls.pack(fill="x")
    query=tk.StringVar()
    page=[1]
    size=50
    ttk.Label(controls,text="检索原文/译文").pack(side="left")
    input_=ttk.Entry(controls,textvariable=query,width=28)
    input_.pack(side="left",padx=8)
    page_label=ttk.Label(controls,text="1 / 1")
    page_label.pack(side="right",padx=6)
    cols=("time","source","translation","language","engine")
    table=ttk.Treeview(win,columns=cols,show="headings")
    for key,label,width in zip(cols,("时间","原文","译文","目标语言","引擎"),
                               (155,250,360,85,110)):
        table.heading(key,text=label)
        table.column(key,width=width,anchor="w")
    table.pack(fill="both",expand=True,padx=8,pady=3)
    max_page=[1]

    def refresh():
        rows,max_page[0]=store.search(query.get(),page[0],size)
        if page[0]>max_page[0]:
            page[0]=max_page[0]
            rows,_=store.search(query.get(),page[0],size)
        for item in table.get_children():
            table.delete(item)
        for entry in rows:
            table.insert("","end",values=(entry.timestamp,entry.source,
                 entry.translated,entry.target,entry.engine))
        page_label["text"]=f"{page[0]} / {max_page[0]} （共 {store.count(query.get())} 条）"

    def find(event=None):
        page[0]=1
        refresh()

    def move(delta):
        page[0]=max(1,min(max_page[0],page[0]+delta))
        refresh()

    def erase():
        if messagebox.askyesno("清空翻译历史","确认永久清空全部翻译历史吗？",parent=win):
            store.clear()
            page[0]=1
            refresh()

    def export():
        filename=filedialog.asksaveasfilename(parent=win,title="导出全部翻译历史",
                 defaultextension=".csv",filetypes=[("CSV","*.csv")])
        if not filename:
            return
        try:
            count=store.export_csv(filename)
            messagebox.showinfo("导出完成",f"已导出 {count} 条记录",parent=win)
        except OSError as exc:
            messagebox.showerror("导出失败",str(exc),parent=win)

    ttk.Button(controls,text="搜索",command=find).pack(side="left")
    input_.bind("<Return>",find)
    buttons=ttk.Frame(win,padding=8)
    buttons.pack(fill="x")
    ttk.Button(buttons,text="上一页",command=lambda:move(-1)).pack(side="left")
    ttk.Button(buttons,text="下一页",command=lambda:move(1)).pack(side="left",padx=6)
    ttk.Button(buttons,text="刷新",command=refresh).pack(side="left",padx=6)
    ttk.Button(buttons,text="导出全部 CSV",command=export).pack(side="left",padx=6)
    ttk.Button(buttons,text="清空历史",command=erase).pack(side="left",padx=6)
    ttk.Button(buttons,text="关闭",command=win.destroy).pack(side="right")
    refresh()
    return win

