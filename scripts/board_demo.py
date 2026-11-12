"""生成记忆看板的演示档案（全部虚构）：python scripts/board_demo.py data/board-demo.sqlite"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xuyi import index
from xuyi.retrieval import Retriever
from xuyi.store import Store

ALICE = 'qq:c2c:' + 'a' * 64
BOB = 'qq:c2c:' + 'b' * 64
DAYS = [
    [('以后叫我小鱼就行。', '好呀小鱼～'), ('我家的猫叫年糕，超级粘人。', '年糕这名字真萌！'),
     ('我最喜欢喝奶茶了，基本每天一杯。', '奶茶党握手～'), ('我对芒果过敏。', '记住啦，芒果不碰。')],
    [('周六我和小林约好去爬山。', '爬山好呀，记得带水。'), ('数学考试下周考，好慌。', '你可以的！'),
     ('我打算周末去宜家买个书架。', '宜家的肉丸也可以吃一下'), ('最近工作压力好大，晚上一直失眠。', '抱抱，要不要聊聊？')],
    [('更正一下，我家的猫不叫年糕，叫汤圆，之前说错了。', '好嘞，汤圆！'), ('奶茶我戒了，现在改喝美式。', '自律了！'),
     ('我最喜欢的颜色是蓝色。', '蓝色很衬你'), ('约了阿珍周日去看电影。', '看什么片呀')],
    [('周六爬山取消了，小林临时要加班。', '那周六好好休息。'), ('阿珍说周日不行，改到下周六了。', '好事多磨～'),
     ('我最喜欢听周杰伦的歌。', '晴天还是七里香？'), ('我最爱吃火锅。', '下次一起涮！'),
     ('我最爱吃寿司。', '寿司也好吃～')],
    # 最近的提问：和真实使用一样，问题本身也是入档的用户消息
    [('我能吃芒果吗？', '不能哦，你对芒果过敏。'), ('你还记得我最喜欢喝什么吗？', '美式，你戒了奶茶。'),
     ('我家猫叫啥来着？', '汤圆呀。')],
]


def main():
    path = Path(sys.argv[1] if len(sys.argv) > 1 else 'data/board-demo.sqlite')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    start = datetime.now(timezone(timedelta(hours=8))) - timedelta(days=len(DAYS) + 18)
    with Store(str(path)) as store:
        for scope, peer, days in ((ALICE, 'a' * 64, DAYS), (BOB, 'b' * 64, [[('叫我阿杰吧。', '好的阿杰')]])):
            rows, seq = [], 0
            for d, turns in enumerate(days):
                session = f's{d}'
                for said, reply in turns:
                    for role, text in (('user', said), ('assistant', reply)):
                        seq += 1
                        rows.append({'scope': scope, 'event_id': f'dsh:{session}:{seq}', 'role': role,
                                     'sender_id': peer if role == 'user' else 'assistant',
                                     'origin': 'quoted' if role == 'user' else 'assistant',
                                     'recorded_at': (start + timedelta(days=6 * d, minutes=seq)).isoformat(),
                                     'text': text})
            store.add_many(rows)
            index.rebuild(store, scope)
        retriever = Retriever(store)
        for question in ('我能吃芒果吗？', '你还记得我最喜欢喝什么吗？', '我家猫叫啥来着？'):
            retriever.recall(ALICE, question, 900, highlight_only=True)
    print(path)


if __name__ == '__main__':
    main()
