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
        pid = len(self.players)
        self.players.append({
            "sid": sid,
            "name": name,
            "hand": [],
            "score": 0,
            "id": pid,
            "is_cpu": is_cpu
        })
        if not is_cpu:
            self.add_log(f"👋 {name} joined.")
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

        for p in self.players:
            p["hand"] = []
        
        for _ in range(5):
            for i in range(self.num_players):
                if self.deck:
                    self.players[i]["hand"].append(self.deck.pop())

        for p in self.players:
            self.sort_hand(p["hand"])

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
        self.add_log(f"Deck remaining: {len(self.deck)}")

        # 親がCPUなら思考開始
        if dealer.get('is_cpu'):
            print(f"[DEBUG] Dealer is CPU ({dealer['name']}). Starting turn...")
            socketio.start_background_task(self.run_cpu_turn, dealer['sid'])

    def get_player_name(self, idx):
        return self.players[idx]['name']

    def add_log(self, message):
        self.logs.append(message)

    def sort_hand(self, hand):
        hand.sort(key=lambda x: (SORT_MAP.get(x["rank"], 99), x["suit"]))

    def draw_all(self):
        if not self.deck: 
            self.add_log("Deck is empty. No draw.")
            return
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
            loss = 0
            if is_tenhou:
                loss = 10
            else:
                base = sum(2 if c["rank"] == 99 else 1 for c in p["hand"])
                if i == self.parent_idx:
                    loss = math.ceil(base * 1.5)
                else:
                    loss = base
            p["score"] -= loss
            total_lost += loss
        self.players[winner_idx]["score"] += total_lost
        self.parent_idx = next_parent
        self.add_log(f"🏆 Winner: {self.get_player_name(winner_idx)}! (+{total_lost} pts)")

    def analyze_hand_composition(self, cards):
        if not cards: return None
        non_jokers = [c for c in cards if c["rank"] != 99]
        joker_count = len(cards) - len(non_jokers)
        total_len = len(cards)

        if any(c["rank"] == 15 for c in non_jokers):
            if total_len > 1: return None 

        if not non_jokers:
            return {'type': 'pair', 'rank': 99, 'len': total_len}

        non_jokers.sort(key=lambda x: SORT_MAP.get(x["rank"], 0))
        min_r = non_jokers[0]["rank"]
        max_r = non_jokers[-1]["rank"]
        
        if all(c["rank"] == min_r for c in non_jokers):
            return {'type': 'pair' if total_len > 1 else 'single', 'rank': min_r, 'len': total_len}

        if total_len >= 3:
            ranks = [c["rank"] for c in non_jokers]
            if len(set(ranks)) == len(ranks):
                needed_span = (max_r - min_r + 1)
                missing_cards = needed_span - len(non_jokers)
                if joker_count >= missing_cards:
                     unused = joker_count - missing_cards
                     start_rank = max(3, min_r - unused)
                     return {'type': 'stairs', 'rank': start_rank, 'len': total_len}

        if total_len >= 4 and total_len % 2 == 0:
            from collections import Counter
            counts = Counter(c["rank"] for c in non_jokers)
            if not any(v > 2 for v in counts.values()):
                needed_jokers_for_fill = 0
                for r in range(min_r, max_r + 1):
                    needed_jokers_for_fill += (2 - counts.get(r, 0))
                if joker_count >= needed_jokers_for_fill:
                    current_span_cards = (max_r - min_r + 1) * 2
                    remaining_cards_needed = total_len - current_span_cards
                    remaining_jokers = joker_count - needed_jokers_for_fill
                    if remaining_cards_needed >= 0 and remaining_jokers == remaining_cards_needed:
                         pairs_below = remaining_cards_needed // 2
                         start_rank = max(3, min_r - pairs_below)
                         return {'type': 'paired_stairs', 'rank': start_rank, 'len': total_len}
        return None

    def is_valid_play(self, cards):
        if not cards: return False
        
        has_two = any(c["rank"] == 15 for c in cards)
        if has_two:
            if len(cards) != 1: return False
            if not self.field: return True
            if len(self.field) == 1: return True
            return False

        if len(cards) == 1 and cards[0]["rank"] == 99: return False
        
        comp = self.analyze_hand_composition(cards)
        if not comp: return False
        c_rank, c_type, c_len = comp['rank'], comp['type'], comp['len']

        if c_rank == 99 and self.field:
            f_non_jokers = [c for c in self.field if c["rank"] != 99]
            if f_non_jokers:
                f_non_jokers.sort(key=lambda x: SORT_MAP.get(x["rank"], 0))
                if f_non_jokers[0]["rank"] == 14: return False

        if not self.field: return True
        if c_len != len(self.field): return False
        
        target_type = self.field_type
        if target_type in ['single', 'pair'] and c_type in ['single', 'pair']: pass
        elif target_type != c_type: return False

        f_non_jokers = [c for c in self.field if c["rank"] != 99]
        if not f_non_jokers: f_rank = 99 
        else:
             f_non_jokers.sort(key=lambda x: SORT_MAP.get(x["rank"], 0))
             f_rank = f_non_jokers[0]["rank"]
        
        if c_rank == 99: return True
        return c_rank == f_rank + 1

    def format_cards_log(self, cards):
        rank_map = {11:'J', 12:'Q', 13:'K', 14:'A', 15:'2', 99:'JK'}
        def r_name(r): return rank_map.get(r, str(r))
        return "[" + ",".join([f"{c['suit']}{r_name(c['rank'])}" for c in cards]) + "]"

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

            has_eight = any(c["rank"] == 8 for c in selected)
            has_two = any(c["rank"] == 15 for c in selected)

            if has_eight or has_two:
                reason = "8-Cut" if has_eight else "2-Power"
                self.add_log(f"⚡ {reason}! Field Cleared.")
                self.draw_all()
                self.field = []
                self.field_type = None
                self.field_owner = None
            else:
                self.turn_idx = (self.turn_idx + 1) % self.num_players

            if not p["hand"]:
                if is_tenhou: self.add_log(f"✨ TENHOU by {p['name']}!")
                self.calculate_scores(p_idx, is_tenhou)
                self.game_over = True
            
            # 次のプレイヤーがCPUならタスク実行
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
                self.add_log("🍂 Field Cleared (All passed)")
                self.draw_all()
                self.field = []
                self.field_type = None
                self.field_owner = None
                self.pass_count = 0
            
            if not self.game_over:
                next_p = self.players[self.turn_idx]
                if next_p.get('is_cpu'):
                    socketio.start_background_task(self.run_cpu_turn, next_p['sid'])
            
            return True

    # --- CPU思考ロジック ---
    def run_cpu_turn(self, cpu_sid):
        """CPUのターンを実行 (emitには socketio.emit を使用してコンテキスト外エラーを回避)"""
        with app.app_context():
            try:
                socketio.sleep(1.0)
                if self.game_over: return

                current_p = self.players[self.turn_idx]
                if current_p['sid'] != cpu_sid:
                    return

                p = current_p
                # --- 候補生成 ---
                candidates = []
                hand_indices = list(range(len(p["hand"])))
                
                # 1. Single/Pair
                rank_groups = {}
                for i, c in enumerate(p["hand"]):
                    r = c["rank"]
                    if r not in rank_groups: rank_groups[r] = []
                    rank_groups[r].append(i)
                
                for r, indices in rank_groups.items():
                    if r == 99: continue
                    if r == 15: 
                        for idx in indices: candidates.append((idx,))
                        continue
                    for k in range(1, len(indices)+1):
                        candidates.extend(list(combinations(indices, k)))

                # Joker Pair
                jokers = rank_groups.get(99, [])
                if jokers:
                    base_cands = list(candidates)
                    for cand in base_cands:
                        if p["hand"][cand[0]]["rank"] == 15: continue
                        for j in range(1, len(jokers)+1):
                            candidates.append(cand + tuple(jokers[:j]))

                # 2. Stairs
                non_joker_indices = [i for i, c in enumerate(p["hand"]) if c["rank"] != 99 and c["rank"] != 15]
                unique_ranks = sorted(list(set(p["hand"][i]["rank"] for i in non_joker_indices)))
                search_lengths = [len(self.field)] if self.field else [3, 4, 5]
                
                for length in search_lengths:
                    if length > len(p["hand"]): continue
                    if length < 3: continue
                    if not unique_ranks: continue
                    min_h = unique_ranks[0]
                    max_h = unique_ranks[-1]
                    
                    for start_r in range(min_h, max_h + 2):
                        target_ranks = list(range(start_r, start_r + length))
                        current_candidate_indices = []
                        needed_jokers = 0
                        for tr in target_ranks:
                            found_idx = -1
                            for idx in non_joker_indices:
                                if p["hand"][idx]["rank"] == tr:
                                    found_idx = idx
                                    break
                            if found_idx != -1:
                                current_candidate_indices.append(found_idx)
                            else:
                                needed_jokers += 1
                        
                        if needed_jokers <= len(jokers):
                            candidates.append(tuple(current_candidate_indices + jokers[:needed_jokers]))

                # --- 選択 ---
                valid_move = None
                field_len = len(self.field) if self.field else 0

                for cand in candidates:
                    cand_list = list(cand)
                    sel = [p["hand"][i] for i in cand_list]
                    if self.is_valid_play(sel):
                        valid_move = cand_list
                        break
                
                if not valid_move and field_len >= 3 and len(hand_indices) <= 12:
                     for cand in combinations(hand_indices, field_len):
                        sel = [p["hand"][i] for i in cand]
                        if self.is_valid_play(sel):
                            valid_move = list(cand)
                            break
                
                if valid_move:
                    self.apply_play(cpu_sid, valid_move)
                else:
                    self.apply_pass(cpu_sid)
                
                # 【重要】コンテキスト外からのブロードキャストには socketio.emit を使う
                emit_update(self.room_id)

            except Exception as e:
                print(f"!!! CPU ERROR !!! : {e}")
                traceback.print_exc()
                try:
                    self.apply_pass(cpu_sid)
                    emit_update(self.room_id)
                except:
                    pass

    def get_public_state(self, requester_sid):
        players_public = []
        my_hand = []
        my_idx = -1
        my_score = 0

        for i, p in enumerate(self.players):
            is_me = (p['sid'] == requester_sid)
            if is_me:
                my_hand = p['hand']
                my_idx = i
                my_score = p['score']
            
            players_public.append({
                "id": i,
                "name": p['name'],
                "hand_count": len(p['hand']),
                "score": p['score'],
                "is_me": is_me,
                "is_cpu": p.get('is_cpu', False)
            })

        return {
            "room_id": self.room_id,
            "players": players_public,
            "my_idx": my_idx,
            "my_hand": my_hand,
            "my_score": my_score,
            "field": self.field,
            "field_owner": self.field_owner,
            "turn": self.turn_idx,
            "parent": self.parent_idx,
            "game_over": self.game_over,
            "logs": self.logs,
            "game_started": self.game_started,
            "deck_count": len(self.deck)
        }

rooms = {}

@app.route('/')
def index(): return render_template('index.html')

@socketio.on('join_game')
def on_join(data):
    room_id = data['room']
    username = data['name']
    
    if room_id not in rooms:
        rooms[room_id] = GameState(room_id)
    
    game = rooms[room_id]
    if game.game_started:
        emit('error', {'msg': 'Game already started'})
        return

    join_room(room_id)
    game.add_player(request.sid, username)
    emit_update(room_id)

@socketio.on('start_practice')
def on_practice(data):
    username = data['name']
    room_id = f"practice_{request.sid}"
    
    rooms[room_id] = GameState(room_id)
    game = rooms[room_id]
    join_room(room_id)
    
    game.add_player(request.sid, username, is_cpu=False)
    game.add_player(f"cpu_{room_id}_1", "CPU 1", is_cpu=True)
    game.add_player(f"cpu_{room_id}_2", "CPU 2", is_cpu=True)
    game.add_player(f"cpu_{room_id}_3", "CPU 3", is_cpu=True)
    
    game.start_game()
    emit_update(room_id)

@socketio.on('start_game')
def on_start(data):
    room_id = data['room']
    if room_id in rooms:
        game = rooms[room_id]
        if game.start_game():
            emit_update(room_id)

@socketio.on('play_card')
def on_play(data):
    room_id = data['room']
    indices = data['indices']
    if room_id in rooms:
        game = rooms[room_id]
        p = next((p for p in game.players if p['sid'] == request.sid), None)
        if p:
            selected = [p["hand"][i] for i in indices]
            if game.is_valid_play(selected):
                if game.apply_play(request.sid, indices):
                    emit_update(room_id)
                else:
                    emit('error', {'msg': 'Action failed'})
            else:
                emit('error', {'msg': 'Invalid Move'})

@socketio.on('pass_turn')
def on_pass(data):
    room_id = data['room']
    if room_id in rooms:
        game = rooms[room_id]
        if game.apply_pass(request.sid):
            emit_update(room_id)

@socketio.on('next_game')
def on_next(data):
    room_id = data['room']
    if room_id in rooms:
        game = rooms[room_id]
        game.next_game()
        emit_update(room_id)

@socketio.on('disconnect')
def on_disconnect():
    for rid, game in rooms.items():
        for p in game.players:
            if p['sid'] == request.sid:
                game.remove_player(request.sid)
                emit_update(rid)
                return

def emit_update(room_id):
    if room_id not in rooms: return
    game = rooms[room_id]
    for p in game.players:
        if p.get('is_cpu'): continue
        # 【修正】バックグラウンドからの送信には socketio.emit を使用
        state = game.get_public_state(p['sid'])
        socketio.emit('update_state', state, room=p['sid'])

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5001)