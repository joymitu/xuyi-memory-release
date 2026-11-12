import argparse
import json
from pathlib import Path
import sys
import sqlite3
from .store import Store, MemoryError
from .states import States


def main():
    # 宿主适配器以 UTF-8 收发；Windows 控制台默认代码页会把中文读坏或写不出
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'): stream.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description='续忆本地记忆核心')
    parser.add_argument('--db',default='data/memory.sqlite',help='本地数据库路径')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('init',help='初始化档案')
    ingest=commands.add_parser('ingest',help='原子导入JSON消息列表');ingest.add_argument('file')
    export=commands.add_parser('export',help='导出单一范围');export.add_argument('scope');export.add_argument('file')
    restore=commands.add_parser('restore',help='校验并恢复');restore.add_argument('file')
    recall=commands.add_parser('recall',help='有来源、有预算地回忆')
    recall.add_argument('scope');recall.add_argument('query')
    recall.add_argument('--model',help='本地ONNX模型目录；省略时明确使用词法模式')
    recall.add_argument('--tokenizer',help='预算分词器JSON；省略时预算单位为UTF-8字节')
    recall.add_argument('--budget',type=int,default=6000)
    recall.add_argument('--sender',help='仅查询这个稳定发言人ID')
    recall.add_argument('--highlight-only',action='store_true',help='仅回忆有原话的当前重点')
    recall.add_argument('--related',action='store_true',help='无重点时按话题稀有实体回退一条相关旧话')
    recall.add_argument('--focus',action='store_true',help='只返回与问题有实质共同词的证据（模型主动查询用）')
    recall.add_argument('--ordinal',action='store_true',help='编号事实清单中旧事实附上最新事实原文（序号冲突规则）')
    for name in ('get','forget'):
        command=commands.add_parser(name);command.add_argument('scope');command.add_argument('event_id')
    reindex=commands.add_parser('reindex',help='从原话档案重建倒排索引');reindex.add_argument('scope',help='范围或 all')
    import_file=commands.add_parser('import-file',help='幂等导入消息数组，带来源水位账本')
    import_file.add_argument('file');import_file.add_argument('--source',required=True)
    import_file.add_argument('--batch',type=int,default=500)
    import_file.add_argument('--provenance',help='来源的生成/验证条件说明，随账本保存')
    merge_command=commands.add_parser('merge',help='把导出包合并进另一范围（分身合并）')
    merge_command.add_argument('scope');merge_command.add_argument('file')
    merge_command.add_argument('--provenance',help='合并缘由与验证条件，随账本保存')
    audit_command=commands.add_parser('audit',help='报告近期被自动重点规则跳过的疑似记忆')
    audit_command.add_argument('scope');audit_command.add_argument('--last',type=int,default=200)
    stats_command=commands.add_parser('stats',help='记忆概况统计：范围、家族候选、索引与交互层')
    stats_command.add_argument('scope')
    commands.add_parser('doctor',help='安装体检：环境/模型/档案/适配器/绑定逐项检查')
    commands.add_parser('register',help='登记本项目目录，让宿主适配器在 Windows/macOS 上都能找到它')
    board_command=commands.add_parser('board',help='在本机浏览器打开记忆看板（只读，只监听 127.0.0.1）')
    board_command.add_argument('--port',type=int,default=0)
    board_command.add_argument('--no-open',action='store_true',help='只打印链接，不自动打开浏览器')
    board_command.add_argument('--profile',help='宿主配置目录（读人设用；默认自动找 ~/.dsh/profiles 下最近改过的）')
    board_command.add_argument('--announce',action='store_true',help='启动后输出一行 JSON（url、token），给 DSH 适配器读')
    board_command.add_argument('--parent-pid',type=int,help='这个进程退出时看板跟着退出（DSH 托管时用）')
    commands.add_parser('personas',help='列出内置人设预设')
    persona_command=commands.add_parser('persona',help='打印人设预设全文（可粘贴进宿主人设配置）')
    persona_command.add_argument('name',nargs='?',help='预设名；用自己写的人设时省略，改用 --file')
    persona_command.add_argument('--file',help='自己写的人设文本文件（UTF-8）')
    persona_command.add_argument('--apply',action='store_true',help='直接写进宿主 QQ 配置（先自动备份）')
    persona_command.add_argument('--no-bubbles',action='store_true',help='不改发送方式（保持一整条、流式）')
    persona_command.add_argument('--profile',default=str(Path.home()/'.dsh/profiles/desktop'))
    persona_command.add_argument('--create',action='store_true',help='补丁里还没有 QQ 配置条目时新建一条（QQ 插件已装、未扫码时用）')
    persona_command.add_argument('--group-reply',choices=('mention','all'),help='群里怎么回：mention 只在 @ 她时回；all 每条都回（不填就不改）')
    prepare_command=commands.add_parser('prepare',help='首次运行准备：语义依赖 + 量化中文模型（装进 DSH 时由适配器在后台调用）')
    prepare_command.add_argument('--status',action='store_true',help='只看准备好了没有')
    prepare_command.add_argument('--home',help='用户目录（默认当前用户；测试用）')
    prepare_command.add_argument('--progress-file',help='适配器读取准备进度的文件')
    prepare_command.add_argument('--run-id',help='适配器本次启动的准备编号')
    commands.add_parser('diary-due',help='到点写日记：补前一天、过了钟点写今天（适配器每隔几分钟调用）')
    hour_command=commands.add_parser('diary-hour',help='看或设每天几点写日记（18–23，默认 22）')
    hour_command.add_argument('--set',type=int)
    setup_state=commands.add_parser('setup-state',help='首次引导用：当前人设与可选预设')
    setup_state.add_argument('--profile',default=str(Path.home()/'.dsh/profiles/desktop'))
    consolidate_command=commands.add_parser('consolidate',help='离线巩固：重建索引、漏记审计与可达性探针')
    consolidate_command.add_argument('scope')
    snapshot_command=commands.add_parser('snapshot',help='新会话开场当前重点快照')
    snapshot_command.add_argument('scope');snapshot_command.add_argument('--budget',type=int,default=500)
    snapshot_command.add_argument('--session',help='宿主当前会话编号（取"上次聊到"时排除它；省略则由档案推断）')
    state=commands.add_parser('state');state.add_argument('scope')
    propose=commands.add_parser('propose');propose.add_argument('scope');propose.add_argument('file')
    propose.add_argument('--revision',type=int,required=True)
    rollback=commands.add_parser('rollback');rollback.add_argument('scope');rollback.add_argument('version')
    rollback.add_argument('--revision',type=int,required=True)
    extract=commands.add_parser('extract');extract.add_argument('scope')
    extract.add_argument('--command-json',required=True)
    extract.add_argument('--budget',type=int,default=12000)
    setup_command=commands.add_parser('setup',help='一条命令装好：找宿主配置、备份、装适配器、登记、选人设')
    setup_command.add_argument('--profile',help='宿主 profile 目录（默认自动找配了 QQ 机器人的那个）')
    setup_command.add_argument('--persona',help='顺便套用的人设预设名')
    setup_command.add_argument('--yes',action='store_true',help='不提问（给脚本用）')
    args=parser.parse_args()
    if args.command=='prepare':
        # 不碰记忆档案（不打开 --db）：只装依赖、下模型。
        from . import prepare as preparing
        result=preparing.status(args.home or Path.home()) if args.status else preparing.prepare(
            args.home, progress_path=args.progress_file, run_id=args.run_id)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if args.status or (result.get('deps') and result.get('model')) else 1
    if args.command=='setup':
        from .setup import run
        try:
            result=run(profile_dir=args.profile,persona=args.persona,yes=args.yes)
        except Exception as exc:
            print(f'错误：{exc}',file=sys.stderr);return 1
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    try:
        with Store(args.db) as store:
            if args.command=='init':result={'状态':'已初始化','数据库':str(Path(args.db).resolve())}
            elif args.command=='ingest':
                incoming=json.loads(sys.stdin.read() if args.file == '-' else Path(args.file).read_text(encoding="utf-8"))
                results=store.add_many(incoming)
                from . import index
                for scope in {event['scope'] for event,status in zip(incoming,results) if status=='inserted'}:
                    index.update(store,scope,[event for event,status in zip(incoming,results)
                                              if status=='inserted' and event['scope']==scope])
                inserted=[event for event,status in zip(incoming,results) if status=='inserted']
                try:
                    from .strength import apply_events
                    for scope in {event['scope'] for event in inserted}:
                        apply_events(store,scope,[event for event in inserted if event['scope']==scope])
                except Exception as exc:
                    print(f'提示：强度表增量更新失败：{type(exc).__name__}: {exc}',file=sys.stderr)
                # 到点写日记：补没写成的前一天；过了写日记的钟点，今天也写（一天只写一次，写完定稿）。
                # 写日记请求是附带的：失败只记一行，原话已经存下，存档本身算成功。
                try:
                    from .board_bridge import prepare_due
                    prepare_due(store,args.db,{event['scope'] for event in incoming})
                except Exception as exc:
                    print(f'提示：日记请求未写出：{type(exc).__name__}: {exc}',file=sys.stderr)
                result=results
            elif args.command=='export':
                bundle=store.export_bundle(args.scope);Path(args.file).write_text(json.dumps(bundle,ensure_ascii=False,indent=2), encoding="utf-8");result={'状态':'已导出','文件':args.file}
            elif args.command=='restore':result=store.restore(json.loads(Path(args.file).read_text(encoding="utf-8")))
            elif args.command=='reindex':
                from . import index
                scopes=[args.scope] if args.scope!='all' else store.scopes()
                for scope in scopes:index.rebuild(store,scope)
                result={'状态':'已重建','范围':scopes}
            elif args.command=='import-file':
                from .importer import import_events, ledger
                events=json.loads(sys.stdin.read() if args.file == '-' else Path(args.file).read_text(encoding="utf-8"))
                result=import_events(store,args.source,events,batch=args.batch,provenance=args.provenance)
                result['ledger']=ledger(store,args.source)
            elif args.command=='merge':
                from .merge import merge_bundle
                bundle=json.loads(Path(args.file).read_text(encoding="utf-8"))
                result=merge_bundle(store,bundle,args.scope,provenance=args.provenance)
            elif args.command=='audit':
                from .audit import audit
                result=audit(store,args.scope,args.last)
            elif args.command=='board':
                from .board_server import serve
                serve(args.db,args.port,not args.no_open and not args.announce,profile_root=args.profile,
                      announce=args.announce,parent_pid=args.parent_pid);return 0
            elif args.command=='register':
                from .doctor import register
                result=register()
            elif args.command=='doctor':
                from .doctor import doctor
                result=doctor()
            elif args.command=='personas':
                from .personas import list_personas
                result=list_personas()
            elif args.command=='setup-state':
                from .personas import list_personas, persona_status
                from .board_bridge import DIARY_HOURS, diary_hour
                from .carry import health
                result={'persona':persona_status(args.profile),'presets':list_personas(include_prompt=True),
                        'diary_hour':diary_hour(args.db),'diary_hours':list(DIARY_HOURS),
                        'health':health(store)}
            elif args.command=='diary-due':
                try:
                    from .strength import rebuild as rebuild_strength
                    rebuild_strength(store,set(store.scopes()))
                except Exception as exc:
                    print(f'提示：强度表刷新失败：{type(exc).__name__}: {exc}',file=sys.stderr)
                from .board_bridge import prepare_due
                result={'requested':prepare_due(store,args.db,set(store.scopes()))}
            elif args.command=='diary-hour':
                from .board_bridge import diary_hour, set_diary_hour
                try:
                    if args.set is not None: set_diary_hour(args.db,args.set)
                except ValueError as exc:
                    raise MemoryError(str(exc)) from exc
                result={'diary_hour':diary_hour(args.db)}
            elif args.command=='persona':
                from .personas import load_persona, apply_persona, compose_prompt
                if bool(args.name)==bool(args.file):raise MemoryError('请给一个预设名，或用 --file 指定自己写的人设（二选一）')
                text=Path(args.file).read_text(encoding='utf-8') if args.file else None
                if args.apply:
                    result=apply_persona(args.name,args.profile,text=text,bubbles=not args.no_bubbles,create=args.create,group_reply=args.group_reply)
                    result['提示']='重启 DeepSeek Harness 后生效'
                else:
                    data=load_persona(args.name) if args.name else {'label':'自定义人设','systemPrompt':text}
                    result=dict(data,directPrompt=compose_prompt(data['systemPrompt']))
            elif args.command=='stats':
                from .stats import stats
                result=stats(store,args.scope)
            elif args.command=='consolidate':
                from .consolidate import consolidate
                result=consolidate(store,args.scope)
            elif args.command=='snapshot':
                from .consolidate import snapshot
                result=snapshot(store,args.scope,args.budget,current_session=args.session)
            elif args.command=='get':
                result=store.get(args.scope,args.event_id)
            elif args.command=='forget':
                store.forget(args.scope,args.event_id);result={'状态':'已删除并记录删除标识'}
            elif args.command=='state':
                result=States(store).view(args.scope)
            elif args.command=='propose':
                result=States(store).propose(args.scope,json.loads(Path(args.file).read_text(encoding="utf-8")),args.revision)
            elif args.command=='rollback':
                result=States(store).rollback(args.scope,None if args.version=='none' else args.version,args.revision)
            elif args.command=='extract':
                from .extraction import extract
                result=extract(store,args.scope,json.loads(args.command_json),budget=args.budget)
            elif args.command=='recall':
                from .embeddings import OnnxEmbedder,TokenizerCounter
                from .retrieval import Retriever
                # 模型或语义依赖缺失（装进 DSH 的包首次运行时就是这样）时退回词法检索，不报错。
                embedder=None;fallback=None
                if args.model:
                    try:embedder=OnnxEmbedder(args.model)
                    except (MemoryError,OSError,ValueError,KeyError) as exc:
                        fallback=f'语义模型不可用，已改用词法检索：{exc}'
                counter=TokenizerCounter(args.tokenizer) if args.tokenizer else None
                result=Retriever(store,embedder,counter).recall(args.scope,args.query,args.budget,args.sender,
                    highlight_only=args.highlight_only,related=args.related,ordinal=args.ordinal,focus=args.focus)
                if fallback and isinstance(result.get('warnings'),list):result['warnings'].insert(0,fallback)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (MemoryError,OSError,sqlite3.Error,json.JSONDecodeError) as exc:
        print(f'错误：{exc}',file=sys.stderr);return 1
    return 0
