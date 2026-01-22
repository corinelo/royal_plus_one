import time
import math
import random
import threading
import traceback
from itertools import combinations
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import eventlet

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins='*')

# --- グローバル変数 & ヘルパー ---
rooms = {}

def emit_update(room_id):
    if room_id not in rooms: return
    game = rooms[room_id]
    for p in game.players:
        if p.get('is_cpu'): continue
        state = game.get_public_state(p['sid'])
        socketio.emit('update_state', state, room=p['sid'])

@socketio.on('send_stamp')
def on_stamp(data):
    room_id = data['room']
    if room_id in rooms:
        socketio.emit('receive_stamp', {'sid': request.sid, 'stamp_id': data['stamp_id']}, room=room_id)

# --- ゲーム定数 ---
SUITS = ['♠', '♥', '♦', '♣']
RANKS = list(range(3, 16))
# 3=3... A=14, 2=15, JK=99
SORT_MAP = {
    3: 0, 4: 1, 5: 2, 6: 3, 7: 4, 8: 5, 9: 6, 10: 7, 
    11: 8, 12: 9, 13: 10, 14: 11, 15: 12, 99: 13
}

class GameState:
    def __init__(self, room_id):
        self.room_id = room_id
        self.players = [] 
        self.max_players = 4
        self.deck = []
        self.field = []
        self.field_type = None
        self.field_owner = None
        self.turn_idx = 0
        self.parent_idx = 0
        self.pass_count = 0
        self.game_started = False
        self.game_over = False
        self.logs = []
        self.lock = threading.Lock()

    def add_player(self, sid, name, is_cpu=False):
        if len(self.players) >= self.max_players: return False
        self.players.append({
            "sid": sid, "name": name, "hand": [], "score": 0,
            "id": len(self.players), "is_cpu": is_cpu
        })
        if not is_cpu: self.add_log(f"👋 {name} joined.")
        return True

    def remove_player(self, sid):
        self.players = [p for p in self.players if p['sid'] != sid]
        self.game_started = False

    def start_game(self):
        with self.lock:
            if len(self.players) < 2: return False
            self.game_started = True
            self.game_over = False
            self.init_round(keep_scores=False)
            return True
    
    def next_game(self):
        with self.lock:
            self.init_round(keep_scores=True)

    def init_round(self, keep_scores=False):
        if not keep_scores:
            for p in self.players: p['score'] = 0
        
        self.num_players = len(self.players)
        self.deck = [{"suit": s, "rank": r} for s in SUITS for r in RANKS]
        self.deck.append({"suit": "JK", "rank": 99})
        self.deck.append({"suit": "JK", "rank": 99})
        random.shuffle(self.deck)

        for p in self.players: p["hand"] = []
        # 初期手札5枚
        for _ in range(5):
            for i in range(self.num_players):
                if self.deck: self.players[i]["hand"].append(self.deck.pop())

        for p in self.players: self.sort_hand(p["hand"])

        self.field = []
        self.field_type = None
        self.field_owner = None
        self.turn_idx = self.parent_idx
        self.pass_count = 0
        self.is_first_turn = True
        self.game_over = False
        self.logs = []
        
        dealer = self.players[self.parent_idx]
        self.add_log(f"--- Game Start (Dealer: {dealer['name']}) ---")
        
        if dealer.get('is_cpu'):
            socketio.start_background_task(self.run_cpu_turn, dealer['sid'])

    def add_log(self, message): self.logs.append(message)
    def sort_hand(self, hand): hand.sort(key=lambda x: (SORT_MAP.get(int(x["rank"]), 99), x["suit"]))

    def draw_all(self, cause_sid=None):
        """
        全員1枚ドロー。
        cause_sid: 場を流した原因となったプレイヤーのSID。このプレイヤーのみ演出フラグ(dramatic=True)を送る。
        """
        if not self.deck: return
        self.add_log("Draw Phase (All players draw 1 card)")
        
        for i in range(self.num_players):
            idx = (self.parent_idx + i) % self.num_players
            if self.deck:
                card = self.deck.pop()
                p = self.players[idx]
                p["hand"].append(card)
                self.sort_hand(p["hand"])
                
                # 個別に通知 (演出用)
                if not p.get('is_cpu'):
                    # 自分が原因の場合のみドラマチック演出
                    is_dramatic = (cause_sid is not None) and (p['sid'] == cause_sid)
                    socketio.emit('player_drew', {'card': card, 'dramatic': is_dramatic}, room=p['sid'])

    def calculate_scores(self, winner_idx, is_tenhou=False):
        total_lost = 0
        next_parent = winner_idx
        for i, p in enumerate(self.players):
            if i == winner_idx: continue
            loss = sum(2 if c["rank"] == 99 else 1 for c in p["hand"])
            if is_tenhou: loss = 10 
            if not is_tenhou and i == self.parent_idx:
                loss = math.ceil(loss * 1.5)
            p["score"] -= loss
            total_lost += loss
            
        self.players[winner_idx]["score"] += total_lost
        self.parent_idx = next_parent
        self.add_log(f"🏆 Winner: {self.players[winner_idx]['name']}! (+{total_lost} pts)")

    def analyze_hand_composition(self, cards):
        if not cards: return None
        for c in cards: c["rank"] = int(c["rank"])
        non_jokers = [c for c in cards if c["rank"] != 99]
        joker_count = len(cards) - len(non_jokers)
        total_len = len(cards)

        # [2] (Rank 15) Check
        if any(c["rank"] == 15 for c in non_jokers):
            if total_len > 1: return None 

        # All Jokers?
        if not non_jokers:
            if total_len == 1: return {'type': 'single', 'rank': 99, 'len': 1}
            if total_len == 2: return {'type': 'pair', 'rank': 99, 'len': 2}
            # Jokerのみの階段はFlex無限大
            return {'type': 'stairs', 'rank': 99, 'len': total_len, 'flex': 99} 

        non_jokers.sort(key=lambda x: SORT_MAP.get(x["rank"], 0))
        min_r = non_jokers[0]["rank"]
        max_r = non_jokers[-1]["rank"]
        
        # --- Single / Pair ---
        if all(c["rank"] == min_r for c in non_jokers):
            return {'type': 'pair' if total_len > 1 else 'single', 'rank': min_r, 'len': total_len}

        # --- Stairs (Sequence) ---
        ranks = [c["rank"] for c in non_jokers]
        is_stairs = (len(set(ranks)) == len(ranks))
        if is_stairs and total_len >= 3:
            needed = (max_r - min_r + 1) - len(non_jokers)
            if joker_count >= needed:
                 spare = joker_count - needed
                 # 【修正】Flex計算
                 # spare分だけ開始位置を上にずらしても成立する
                 start_rank = max(3, min_r - spare)
                 return {'type': 'stairs', 'rank': start_rank, 'len': total_len, 'flex': spare}

        # --- Pair Stairs (Sequence of Pairs) ---
        if total_len >= 4 and total_len % 2 == 0:
            from collections import Counter
            counts = Counter(ranks)
            if all(v <= 2 for v in counts.values()):
                unique_ranks = sorted(counts.keys())
                u_min, u_max = unique_ranks[0], unique_ranks[-1]
                pairs_needed = total_len // 2
                
                start_candidate_min = max(3, u_max - pairs_needed + 1)
                start_candidate_max = u_min
                
                for s_rank in range(start_candidate_min, start_candidate_max + 1):
                    cost = 0
                    valid_range = True
                    for i in range(pairs_needed):
                        r = s_rank + i
                        if r == 15: 
                            valid_range = False; break
                        has_count = counts.get(r, 0)
                        cost += (2 - has_count)
                    
                    if valid_range and cost <= joker_count:
                        # ここも厳密にはFlex可能だが、一旦固定
                        return {'type': 'pair_stairs', 'rank': s_rank, 'len': total_len}

        return None

    def is_valid_play(self, cards):
        if not cards: return False
        
        if len(cards) == 1 and int(cards[0]["rank"]) == 15:
            if not self.field: return True
            if len(self.field) == 1: return True
            return False 
        
        if len(cards) == 1 and int(cards[0]["rank"]) == 99: return False 
        
        comp = self.analyze_hand_composition(cards)
        if not comp: return False
        
        if not self.field: return True
        
        if comp['len'] != len(self.field): return False
        
        f_type = self.field_type
        c_type = comp['type']
        
        if f_type != c_type:
            # 自分がAll Jokerなら相手に合わせる
            if all(c['rank']==99 for c in cards): pass 
            else: return False

        f_non_jokers = [c for c in self.field if int(c["rank"]) != 99]
        if not f_non_jokers: 
            f_res = self.analyze_hand_composition(self.field)
            f_rank = f_res['rank'] if f_res else 99
        else:
            f_res = self.analyze_hand_composition(self.field)
            f_rank = f_res['rank'] if f_res else 99

        c_rank = comp['rank']
        if c_rank == 99: return True 
        if f_rank == 14 and c_rank != 15: return False

        # 【修正】階段のFlex対応
        # 場: Rank X -> 次は Rank X+1
        # 自分: Rank Y 〜 Y+Flex
        # 要求: X+1 が [Y, Y+Flex] に含まれればOK
        if c_type == 'stairs':
            target = f_rank + 1
            c_min = c_rank
            c_max = c_rank + comp.get('flex', 0)
            if c_min <= target <= c_max:
                return True
            return False

        return c_rank == f_rank + 1

    def format_cards_log(self, cards):
        rmap = {11:'J', 12:'Q', 13:'K', 14:'A', 15:'2', 99:'JK'}
        return "[" + ",".join([f"{c['suit']}{rmap.get(c['rank'], str(c['rank']))}" for c in cards]) + "]"

    def apply_play(self, sid, indices):
        with self.lock:
            p_idx = -1
            for i, p in enumerate(self.players):
                if p['sid'] == sid: p_idx = i
            if p_idx != self.turn_idx: return False

            p = self.players[p_idx]
            selected = [p["hand"][i] for i in indices]
            is_tenhou = (self.is_first_turn and p_idx == self.parent_idx and len(selected) == 5)
            
            for i in sorted(indices, reverse=True): p["hand"].pop(i)
            self.add_log(f"{p['name']} played {self.format_cards_log(selected)}")

            comp = self.analyze_hand_composition(selected)
            if comp and not self.field: 
                self.field_type = comp['type']
            
            self.field = selected
            self.field_owner = p_idx
            self.pass_count = 0
            self.is_first_turn = False

            has_8_or_2 = any(c["rank"] in [8, 15] for c in selected)
            if has_8_or_2:
                self.add_log(f"⚡ {'8-Cut' if any(c['rank']==8 for c in selected) else '2-Power'}!")
                emit_update(self.room_id)
                socketio.sleep(1.0)
                # 8/2を出した本人がCause
                self.draw_all(cause_sid=sid)
                self.field = []
                self.field_type = None
                self.field_owner = None
            else:
                self.turn_idx = (self.turn_idx + 1) % self.num_players

            if not p["hand"]:
                if is_tenhou: self.add_log(f"✨ TENHOU by {p['name']}!")
                self.calculate_scores(p_idx, is_tenhou)
                self.game_over = True
            
            if not self.game_over:
                next_p = self.players[self.turn_idx]
                if next_p.get('is_cpu'):
                    socketio.start_background_task(self.run_cpu_turn, next_p['sid'])
            return True

    def apply_pass(self, sid):
        with self.lock:
            p_idx = -1
            for i, p in enumerate(self.players):
                if p['sid'] == sid: p_idx = i
            if p_idx != self.turn_idx: return False

            self.add_log(f"{self.players[p_idx]['name']} passed.")
            self.pass_count += 1
            self.turn_idx = (self.turn_idx + 1) % self.num_players
            self.is_first_turn = False
            
            if self.pass_count >= self.num_players - 1:
                self.add_log("🍂 Field Cleared")
                emit_update(self.room_id)
                socketio.sleep(1.0)
                
                # 全員パスで流れた場合、最後にカードを出していた人がCause
                cause_sid = None
                if self.field_owner is not None and 0 <= self.field_owner < len(self.players):
                    cause_sid = self.players[self.field_owner]['sid']

                self.draw_all(cause_sid=cause_sid)
                self.field = []
                self.field_type = None
                self.field_owner = None
                self.pass_count = 0
            
            if not self.game_over:
                next_p = self.players[self.turn_idx]
                if next_p.get('is_cpu'):
                    socketio.start_background_task(self.run_cpu_turn, next_p['sid'])
            return True

    def run_cpu_turn(self, cpu_sid):
        with app.app_context():
            try:
                socketio.sleep(1.5)
                if self.game_over: return
                p = self.players[self.turn_idx]
                if p['sid'] != cpu_sid: return

                hand = p["hand"]
                n = len(hand)
                valid_moves = []
                search_sizes = [len(self.field)] if self.field else range(1, min(n + 1, 6))
                
                indices = list(range(n))
                for size in search_sizes:
                    for combo in combinations(indices, size):
                        sel = [hand[i] for i in combo]
                        if self.is_valid_play(sel):
                            valid_moves.append(list(combo))
                
                best_move = None
                if valid_moves:
                    if not self.field:
                        valid_moves.sort(key=lambda m: (-len(m), sum(hand[i]['rank'] for i in m)))
                    else:
                        valid_moves.sort(key=lambda m: sum(hand[i]['rank'] for i in m))
                    best_move = valid_moves[0]

                if best_move:
                    self.apply_play(cpu_sid, best_move)
                else:
                    self.apply_pass(cpu_sid)
                
                emit_update(self.room_id)
            except Exception as e:
                print(f"CPU ERROR: {e}")
                traceback.print_exc()
                self.apply_pass(cpu_sid)
                emit_update(self.room_id)

    def get_public_state(self, requester_sid):
        players_public = []
        my_hand = []; my_idx = -1; my_score = 0
        for i, p in enumerate(self.players):
            is_me = (p['sid'] == requester_sid)
            if is_me: my_hand = p['hand']; my_idx = i; my_score = p['score']
            players_public.append({
                "id": i, "name": p['name'], "hand_count": len(p['hand']),
                "score": p['score'], "is_me": is_me, "is_cpu": p.get('is_cpu')
            })
        return {
            "room_id": self.room_id, "players": players_public,
            "my_idx": my_idx, "my_hand": my_hand, "my_score": my_score,
            "field": self.field, "field_type": self.field_type, "field_owner": self.field_owner,
            "turn": self.turn_idx, "parent": self.parent_idx,
            "game_over": self.game_over, "logs": self.logs,
            "game_started": self.game_started, "deck_count": len(self.deck)
        }

@app.route('/')
def index(): return render_template('index.html')

@socketio.on('join_game')
def on_join(data):
    room_id = data['room']
    if room_id not in rooms: rooms[room_id] = GameState(room_id)
    game = rooms[room_id]
    existing = next((p for p in game.players if p['name'] == data['name']), None)
    if existing:
        existing['sid'] = request.sid 
        game.add_log(f"🔄 {data['name']} reconnected.")
    else:
        if not game.game_started:
            game.add_player(request.sid, data['name'])
    join_room(room_id)
    emit_update(room_id)

@socketio.on('start_practice')
def on_practice(data):
    room = f"practice_{request.sid}"
    rooms[room] = GameState(room)
    join_room(room)
    rooms[room].add_player(request.sid, data['name'])
    for i in range(1,4): rooms[room].add_player(f"cpu_{room}_{i}", f"CPU {i}", True)
    rooms[room].start_game()
    emit_update(room)

@socketio.on('start_game')
def on_start(data):
    if data['room'] in rooms:
        if rooms[data['room']].start_game(): emit_update(data['room'])

@socketio.on('play_card')
def on_play(data):
    room_id = data['room']
    if room_id in rooms:
        game = rooms[room_id]
        p = next((p for p in game.players if p['sid'] == request.sid), None)
        if p:
            indices = data['indices']
            if game.is_valid_play([p["hand"][i] for i in indices]):
                if game.apply_play(request.sid, indices):
                    emit_update(room_id)
                else: emit('error', {'msg': 'Action failed'})
            else: emit('error', {'msg': 'Invalid Move'})

@socketio.on('pass_turn')
def on_pass(data):
    if data['room'] in rooms:
        rooms[data['room']].apply_pass(request.sid)
        emit_update(data['room'])

@socketio.on('next_game')
def on_next(data):
    if data['room'] in rooms:
        rooms[data['room']].next_game()
        emit_update(data['room'])

@socketio.on('reset_game')
def on_reset(data):
    if data['room'] in rooms:
        rooms[data['room']].init_round(False)
        emit_update(data['room'])

@socketio.on('disconnect')
def on_disconnect():
    pass

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5001)
