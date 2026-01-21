import time
import math
import random
import threading
import traceback
from itertools import chain, combinations
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
            # 練習モードなら人数チェック緩和
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
        
        # 親がCPUなら思考開始
        if dealer.get('is_cpu'):
            socketio.start_background_task(self.run_cpu_turn, dealer['sid'])

    def add_log(self, message): self.logs.append(message)
    def sort_hand(self, hand): hand.sort(key=lambda x: (SORT_MAP.get(x["rank"], 99), x["suit"]))

    def draw_all(self):
        if not self.deck: return
        self.add_log("Draw Phase (All players draw 1 card)")
        for i in range(self.num_players):
            idx = (self.parent_idx + i) % self.num_players
            if self.deck:
                card = self.deck.pop()
                self.players[idx]["hand"].append(card)
                self.sort_hand(self.players[idx]["hand"])

    def calculate_scores(self, winner_idx, is_tenhou=False):
        total_lost = 0
        next_parent = winner_idx
        for i, p in enumerate(self.players):
            if i == winner_idx: continue
            loss = 10 if is_tenhou else sum(2 if c["rank"] == 99 else 1 for c in p["hand"])
            if not is_tenhou and i == self.parent_idx: loss = math.ceil(loss * 1.5)
            p["score"] -= loss
            total_lost += loss
        self.players[winner_idx]["score"] += total_lost
        self.parent_idx = next_parent
        self.add_log(f"🏆 Winner: {self.players[winner_idx]['name']}! (+{total_lost} pts)")

    def analyze_hand_composition(self, cards):
        if not cards: return None
        non_jokers = [c for c in cards if c["rank"] != 99]
        joker_count = len(cards) - len(non_jokers)
        total_len = len(cards)
        
        # 2(Rank15)は単体のみ
        if any(c["rank"] == 15 for c in non_jokers):
            if total_len > 1: return None 

        if not non_jokers: return {'type': 'pair', 'rank': 99, 'len': total_len}

        non_jokers.sort(key=lambda x: SORT_MAP.get(x["rank"], 0))
        min_r, max_r = non_jokers[0]["rank"], non_jokers[-1]["rank"]
        
        if all(c["rank"] == min_r for c in non_jokers):
            return {'type': 'pair' if total_len > 1 else 'single', 'rank': min_r, 'len': total_len}

        # 階段
        if total_len >= 3:
            ranks = [c["rank"] for c in non_jokers]
            if len(set(ranks)) == len(ranks):
                needed = (max_r - min_r + 1) - len(non_jokers)
                if joker_count >= needed:
                     return {'type': 'stairs', 'rank': max(3, min_r - (joker_count - needed)), 'len': total_len}
        return None

    def is_valid_play(self, cards):
        if not cards: return False
        if any(c["rank"] == 15 for c in cards) and len(cards) > 1: return False
        
        # 2(15)単体は最強
        if len(cards) == 1 and cards[0]["rank"] == 15:
            if not self.field: return True
            if len(self.field) == 1: return True
            return False

        if len(cards) == 1 and cards[0]["rank"] == 99: return False # Joker単体禁止
        
        comp = self.analyze_hand_composition(cards)
        if not comp: return False
        
        # 場のカードとの比較
        if not self.field: return True
        if comp['len'] != len(self.field): return False
        if self.field_type not in ['single', 'pair'] and self.field_type != comp['type']: return False
        
        # 場がJokerのみ(rank99)の場合の特殊処理などは省略(基本rankあり)
        f_non_jokers = [c for c in self.field if c["rank"] != 99]
        f_rank = f_non_jokers[0]["rank"] if f_non_jokers else 99
        
        # 場がA(14)なら次は2(15)かJokerペア等は不可(2は単体のみだから)
        if f_rank == 14 and comp['rank'] != 15:
             # Aの次は出せない（2は単体専用だからペア等では出せない）
             return False

        if comp['rank'] == 99: return True
        return comp['rank'] == f_rank + 1

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
            if comp and not self.field: self.field_type = comp['type']
            
            self.field = selected
            self.field_owner = p_idx
            self.pass_count = 0
            self.is_first_turn = False

            has_8_or_2 = any(c["rank"] in [8, 15] for c in selected)
            if has_8_or_2:
                self.add_log(f"⚡ {'8-Cut' if any(c['rank']==8 for c in selected) else '2-Power'}!")
                emit_update(self.room_id)
                socketio.sleep(1.0)
                self.draw_all()
                self.field = []
                self.field_type = None
            else:
                self.turn_idx = (self.turn_idx + 1) % self.num_players

            if not p["hand"]:
                if is_tenhou: self.add_log(f"✨ TENHOU!")
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
                self.draw_all()
                self.field = []
                self.field_type = None
                self.pass_count = 0
            
            if not self.game_over:
                next_p = self.players[self.turn_idx]
                if next_p.get('is_cpu'):
                    socketio.start_background_task(self.run_cpu_turn, next_p['sid'])
            return True

    # --- CPUロジック (Brute Force) ---
    def run_cpu_turn(self, cpu_sid):
        with app.app_context():
            try:
                socketio.sleep(1.0)
                if self.game_over: return
                p = self.players[self.turn_idx]
                if p['sid'] != cpu_sid: return

                hand = p["hand"]
                n = len(hand)
                valid_moves = []

                # 全組み合わせ探索 (手札枚数が少ないので高速)
                # 場があるときは枚数を合わせるだけでいいので探索範囲を絞れる
                search_sizes = [len(self.field)] if self.field else range(1, min(n + 1, 6))
                
                indices = list(range(n))
                for size in search_sizes:
                    for combo in combinations(indices, size):
                        sel = [hand[i] for i in combo]
                        if self.is_valid_play(sel):
                            valid_moves.append(list(combo))
                
                # 戦略: 親なら枚数が多い方、子なら出せるなら出す
                best_move = None
                if valid_moves:
                    if not self.field:
                        # 親: 枚数が多い > ランクが低い
                        valid_moves.sort(key=lambda m: (len(m), -hand[m[0]]['rank']), reverse=True)
                    else:
                        # 子: なんでもいいから出す (ランクが低い順)
                        valid_moves.sort(key=lambda m: hand[m[0]]['rank'])
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
        my_idx = -1
        my_hand = []
        my_score = 0
        p_list = []
        for i, p in enumerate(self.players):
            is_me = (p['sid'] == requester_sid)
            if is_me:
                my_idx = i
                my_hand = p['hand']
                my_score = p['score']
            p_list.append({
                "id": i, "name": p['name'], "hand_count": len(p['hand']), 
                "score": p['score'], "is_me": is_me, "is_cpu": p.get('is_cpu')
            })
        return {
            "room_id": self.room_id, "players": p_list, "my_idx": my_idx,
            "my_hand": my_hand, "my_score": my_score,
            "field": self.field, "field_owner": self.field_owner,
            "turn": self.turn_idx, "parent": self.parent_idx,
            "game_over": self.game_over, "logs": self.logs,
            "game_started": self.game_started, "deck_count": len(self.deck)
        }

@app.route('/')
def index(): return render_template('index.html')

@socketio.on('join_game')
def on_join(data):
    room, name = data['room'], data['name']
    if room not in rooms: rooms[room] = GameState(room)
    if not rooms[room].game_started:
        join_room(room)
        rooms[room].add_player(request.sid, name)
        emit_update(room)

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
    if data['room'] in rooms:
        rooms[data['room']].apply_play(request.sid, data['indices'])
        emit_update(data['room'])

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
    for r in rooms.values():
        if any(p['sid'] == request.sid for p in r.players):
            r.remove_player(request.sid)
            emit_update(r.room_id)

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5001)
