import os
import sqlite3
import random
import time
import asyncio
import datetime
import uuid
import json
import discord
from discord.ext import commands
from discord import app_commands
from collections import Counter
from dotenv import load_dotenv
# ==========================================
# ⚙️ 설정 & 전역 변수
# ==========================================
load_dotenv()
DB_PATH = "casino_v20_aris.db"
DEVELOPER_IDS = [
    int(x.strip())
    for x in os.getenv("DEVELOPER_IDS", "").split(",")
    if x.strip()
]
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

INTENTS = discord.Intents.default()
INTENTS.message_content = True

GLOBAL_PLAYING_USERS = set()
BLACKJACK_GAMES = {}
DB_LOCK = asyncio.Lock()
STATE_BACKUP_PATH = "casino_state_backup.json"

# ==========================================
# 🛠️ 권한 확인 함수
# ==========================================
def is_host_or_admin(interaction: discord.Interaction, table) -> bool:
    is_host  = interaction.user.id == table.host.id
    is_admin = interaction.user.guild_permissions.administrator
    return is_host or is_admin

def is_authorized_admin(interaction: discord.Interaction) -> bool:
    is_owner     = interaction.guild and (interaction.guild.owner_id == interaction.user.id)
    is_developer = interaction.user.id in DEVELOPER_IDS
    return is_owner or is_developer

# ==========================================
# 💾 데이터베이스 (check_same_thread=False + asyncio.Lock)
# ==========================================
def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        balance INTEGER DEFAULT 10000,
        last_daily TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS game_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        table_id TEXT,
        game_type TEXT,
        entry_cost INTEGER,
        player_count INTEGER,
        winner_id INTEGER,
        prize INTEGER,
        created_at TEXT
    )""")
    cols = ["wins_total INTEGER DEFAULT 0", "games_total INTEGER DEFAULT 0"]
    for cost in [0, 1, 3, 10]:
        for p in range(2, 7):
            cols.append(f"w_{cost}k_{p}p INTEGER DEFAULT 0")
            cols.append(f"gp_{cost}k_{p}p INTEGER DEFAULT 0")
    col_def = ", ".join(cols)
    con.execute(f"CREATE TABLE IF NOT EXISTS stats(user_id INTEGER PRIMARY KEY, {col_def})")
    con.commit()
    con.close()

def ensure_user(uid):
    con = db()
    con.execute("INSERT OR IGNORE INTO users(user_id, balance) VALUES(?, 10000)", (uid,))
    con.execute("INSERT OR IGNORE INTO stats(user_id) VALUES(?)", (uid,))
    con.commit()
    con.close()

def get_user_balance(uid):
    ensure_user(uid)
    con = db()
    res = con.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return res[0] if res else 10000

def set_balance(uid, amount):
    ensure_user(uid)
    con = db()
    con.execute("UPDATE users SET balance = ? WHERE user_id = ?", (amount, uid))
    con.commit()
    con.close()

def set_all_balances(amount):
    con = db()
    con.execute("UPDATE users SET balance = ?", (amount,))
    con.commit()
    con.close()

def update_balance(uid, amount):
    ensure_user(uid)
    con = db()
    con.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, uid))
    con.commit()
    con.close()

def update_all_balances(amount):
    con = db()
    con.execute("UPDATE users SET balance = balance + ?", (amount,))
    con.commit()
    con.close()

def record_game_result(players, winner, entry_cost, table_id, game_type, prize_pool):
    con = db()
    cost_key = int(entry_cost / 1000) if entry_cost >= 1000 else 0
    p_count  = len(players)
    gp_col   = f"gp_{cost_key}k_{p_count}p"
    w_col    = f"w_{cost_key}k_{p_count}p"
    try:
        for p in players:
            ensure_user(p.member.id)
            con.execute(
                f"UPDATE stats SET games_total=games_total+1, {gp_col}={gp_col}+1 WHERE user_id=?",
                (p.member.id,)
            )
        if winner:
            ensure_user(winner.member.id)
            con.execute(
                f"UPDATE stats SET wins_total=wins_total+1, {w_col}={w_col}+1 WHERE user_id=?",
                (winner.member.id,)
            )
        con.execute(
            "INSERT INTO game_log(table_id,game_type,entry_cost,player_count,winner_id,prize,created_at) VALUES(?,?,?,?,?,?,?)",
            (table_id, game_type, entry_cost, p_count,
             winner.member.id if winner else None,
             prize_pool, datetime.datetime.now().isoformat())
        )
        con.commit()
    except Exception as e:
        print(f"[Stats Error] {e}")
    finally:
        con.close()

def get_user_stats_all(uid):
    ensure_user(uid)
    con = db()
    row = con.execute("SELECT * FROM stats WHERE user_id=?", (uid,)).fetchone()
    con.close()
    return row

def reset_stats_db(target_uid=None):
    con = db()
    cols = ["wins_total=0", "games_total=0"]
    for cost in [0, 1, 3, 10]:
        for p in range(2, 7):
            cols.append(f"w_{cost}k_{p}p=0")
            cols.append(f"gp_{cost}k_{p}p=0")
    set_clause = ", ".join(cols)
    if target_uid:
        con.execute(f"UPDATE stats SET {set_clause} WHERE user_id=?", (target_uid,))
    else:
        con.execute(f"UPDATE stats SET {set_clause}")
    con.commit()
    con.close()

def daily_check(uid):
    ensure_user(uid)
    con = db()
    today = datetime.date.today().isoformat()
    last  = con.execute("SELECT last_daily FROM users WHERE user_id=?", (uid,)).fetchone()[0]
    if last == today:
        con.close()
        return False
    con.execute("UPDATE users SET balance=balance+2000, last_daily=? WHERE user_id=?", (today, uid))
    con.commit()
    con.close()
    return True

# ==========================================
# 🃏 카드 & 핸드 평가 공통 로직
# ==========================================
SUITS    = ["♠️", "♥️", "♦️", "♣️"]
RANK_STR = {11: "J", 12: "Q", 13: "K", 14: "A"}

def card_str(card):
    r = card // 4 + 2
    s = card % 4
    return f"[{RANK_STR.get(r, str(r))}{SUITS[s]}]"

def make_deck():     return list(range(52))
def deal_card(deck): return deck.pop()

def eval_hand(cards, use_back_straight=False):
    ranks       = sorted([c // 4 + 2 for c in cards], reverse=True)
    suits       = [c % 4 for c in cards]
    rank_counts = Counter(ranks)
    s_counts    = Counter(suits)

    is_flush   = False
    flush_suit = None
    flush_candidates = [(s, c) for s, c in s_counts.items() if c >= 5]
    if flush_candidates:
        is_flush   = True
        flush_suit = max(
            flush_candidates,
            key=lambda sc: sorted([c // 4 + 2 for c in cards if c % 4 == sc[0]], reverse=True)
        )[0]

    def get_straight(uniq_ranks):
        for r in uniq_ranks:
            if r < 6: break
            if all(x in uniq_ranks for x in range(r, r - 5, -1)): return r
        if {14, 5, 4, 3, 2}.issubset(uniq_ranks): return 5
        return 0

    def adjust_st_score(st):
        if not use_back_straight: return (st,)
        if st == 14: return (14,)
        if st == 5:  return (13,)
        return (st - 1,)

    def find_cards(target_ranks, limit=None):
        found = []
        for r in target_ranks:
            for c in cards:
                if c // 4 + 2 == r and c not in found:
                    found.append(c)
        return found[:limit] if limit else found

    if is_flush:
        f_cards = [c for c in cards if c % 4 == flush_suit]
        f_ranks = sorted(list({c // 4 + 2 for c in f_cards}), reverse=True)
        st = get_straight(f_ranks)
        if st:
            tgt   = [14, 2, 3, 4, 5] if st == 5 else list(range(st, st - 5, -1))
            best5 = []
            for r in tgt:
                for c in f_cards:
                    if c // 4 + 2 == r: best5.append(c); break
            return (8, adjust_st_score(st), best5[:5])

    quads = [r for r, c in rank_counts.items() if c == 4]
    trips = [r for r, c in rank_counts.items() if c == 3]
    pairs = [r for r, c in rank_counts.items() if c == 2]

    if quads:
        k = max(r for r in ranks if r != quads[0])
        return (7, (quads[0], k), find_cards([quads[0]], 4) + find_cards([k], 1))
    if trips and (len(trips) >= 2 or pairs):
        t = max(trips)
        p = max(trips, key=lambda x: x if x != t else -1) if len(trips) >= 2 else max(pairs)
        return (6, (t, p), find_cards([t], 3) + find_cards([p], 2))
    if is_flush:
        f_cards = sorted([c for c in cards if c % 4 == flush_suit], key=lambda x: x // 4 + 2, reverse=True)
        return (5, tuple(c // 4 + 2 for c in f_cards[:5]), f_cards[:5])

    uniq_ranks = sorted(list(set(ranks)), reverse=True)
    st = get_straight(uniq_ranks)
    if st:
        tgt   = [14, 5, 4, 3, 2] if st == 5 else list(range(st, st - 5, -1))
        best5 = []
        for r in tgt:
            for c in cards:
                if c // 4 + 2 == r: best5.append(c); break
        return (4, adjust_st_score(st), best5)

    if trips:
        t  = max(trips)
        ks = sorted([r for r in ranks if r != t], reverse=True)[:2]
        return (3, (t, tuple(ks)), find_cards([t], 3) + find_cards(ks, 2))
    if len(pairs) >= 2:
        pairs_s = sorted(pairs, reverse=True)
        p1, p2  = pairs_s[0], pairs_s[1]
        k = max(r for r in ranks if r not in (p1, p2))
        return (2, (p1, p2, k), find_cards([p1], 2) + find_cards([p2], 2) + find_cards([k], 1))
    if pairs:
        p1 = pairs[0]
        ks = sorted([r for r in ranks if r != p1], reverse=True)[:3]
        return (1, (p1, tuple(ks)), find_cards([p1], 2) + find_cards(ks, 3))

    best5 = sorted(cards, key=lambda x: x // 4 + 2, reverse=True)[:5]
    return (0, tuple(c // 4 + 2 for c in best5), best5)

def hand_name(val):
    return ["High Card", "One Pair", "Two Pair", "Three of a Kind",
            "Straight", "Flush", "Full House", "Four of a Kind", "Straight Flush"][val]

def board_best_hand_hint(community):
    if len(community) < 3:
        return ""
    score, _, _ = eval_hand(community)
    return f"🃏 보드 최강: **{hand_name(score)}**"

# ==========================================
# 🎉 특수 효과: 포카드 이상 핸드 시 표시할 이펙트
# ==========================================
def special_hand_effect(score):
    """포카드(7) 이상의 핸드에 대해 화려한 특수효과 문자열을 반환."""
    if score >= 8:
        return "\n🎆🎇✨ **로열/스트레이트 플러시!! 폭죽이 터집니다!** ✨🎇🎆\n🎉🎊🎉🎊🎉🎊🎉🎊🎉🎊"
    if score == 7:
        return "\n🎆✨ **포카드(Four of a Kind)!! 대박 핸드!** ✨🎆\n🎉🎊🎉🎊🎉"
    return ""

# ==========================================
# 🎴 블랙잭 공통 로직
# ==========================================
def get_bj_score(cards):
    score, aces = 0, 0
    for c in cards:
        rank = c // 4 + 2
        if 11 <= rank <= 13: score += 10
        elif rank == 14:     score += 11; aces += 1
        else:                score += rank
    while score > 21 and aces > 0:
        score -= 10; aces -= 1
    return score

def get_character_dialogue(char_type):
    if char_type == "key":
        return {
            "title":            "⚙️ <Key> 시스템 동기화",
            "start":            "아, 오셨군요.\n이번에도 저를 바쁘게 만들 생각인가요?\n뭐... 어차피 말려도 하실 거잖아요.",
            "next":             "선생님?\n설마 또 고민만 하고 계신 건 아니죠?\n정말이지...",
            "hit":              "🎴 또 받으시겠다고요?\n흥, 좋을 대로 하세요.\n나중에 결과가 이상해져도 전 몰라요.",
            "stand":            "거기서 멈추시는 건가요?\n의외로 무난한 선택이네요.",
            "blackjack":        "어라?\n정말 해내셨네요.\n✨ 블랙잭입니다.\n...흥. 이번만큼은 꽤 멋졌다고 해 둘게요.",
            "big_win":          "흥.\n이 정도면 꽤 잘한 편이네요.\n...칭찬해 달라는 표정은 하지 마세요.",
            "win":              "오...\n생각보다 잘 풀렸네요.\n선생님답지 않게 말이에요.",
            "push":             "흠.\n애매하긴 하지만...\n적어도 망한 건 아니잖아요?",
            "lose":             "하아...\n그러니까 조금 더 신중하게 하라고 했잖아요.\n뭐, 다음 판이 있으니까요.",
            "dealer_blackjack": "......\n아, 이런.\n이건 좀 억울하네요.\n선생님이 못한 것보단 상대 운이 너무 좋았던 것 같아요.",
            "timeout_refund":   "선생님?\n게임 시작하자마자 어디 가신 건가요?\n흥...\n이번은 없던 걸로 해 드릴게요.",
            "timeout_lose":     "정말이지...\n기다리는 것도 한계가 있거든요.\n이번 판은 제가 정리해 버렸어요.\n...다음엔 조금만 더 집중하세요.",
            "dealer_name":      "🤖 케이 (Key)",
            "color":            0x4A69BD,
        }
    return {
        "title":            "🃏 아리스와의 블랙잭 승부",
        "start":            "빠밤! 카드를 나눠드렸습니다. 선생님, 준비되셨나요?",
        "next":             "선생님! 다음 행동을 선택해 주세요! 아리스, 대기 중입니다.",
        "hit":              "🎴 **[드로우!]** 카드를 한 장 뽑았습니다. 현재 스코어 확인 중...",
        "stand":            "✋ 스탠드! 딜러 차례입니다...",
        "blackjack":        "✨ **빠밤! 크리티컬 히트! 블랙잭입니다!** (보상 x1.5배 획득!)",
        "big_win":          "🎉 **와아! 대승리! 선생님 정말 대단해요!!**",
        "win":              "🎉 **퀘스트 클리어! 아리스를 이겼습니다! 선생님은 역시 고수시군요!**",
        "push":             "🤝 **무승부! 비겼습니다. 선생님과 아리스의 실력은 막상막하입니다.** (베팅 반환)",
        "lose":             "💀 **게임 오버... 아리스의 승리입니다! 선생님, 청휘석은 소중히 다뤄야 합니다.**",
        "dealer_blackjack": "💥 딜러 블랙잭 발생!\n히잉...\n이번엔 운이 조금 안 따라줬네요.\n다음 판엔 꼭 이길 수 있을 거예요!",
        "timeout_refund":   "어라?\n선생님이 안 계시네요!\n음...\n이번 게임은 취소하고 칩은 돌려드릴게요!",
        "timeout_lose":     "앗!\n선생님을 기다렸는데 시간이 다 지나 버렸어요...\n이번 판은 패배 처리예요.\n다음엔 같이 끝까지 해요!",
        "dealer_name":      "🤖 아리스 (Aris)",
        "color":            0x3498db,
    }
# ==========================================
# 🎮 블랙잭 View
# ==========================================
class BlackjackCharacterSelectView(discord.ui.View):
    def __init__(self, user, amount):
        super().__init__(timeout=60)
        self.user    = user
        self.amount  = amount
        self.message = None
        self._ended  = False

    async def on_timeout(self):
        if self._ended:
            return
        self._ended = True
        try:
            update_balance(self.user.id, self.amount)
        finally:
            GLOBAL_PLAYING_USERS.discard(self.user.id)
            BLACKJACK_GAMES.pop(self.user.id, None)
        embed = discord.Embed(
            title="⌛ 블랙잭 시간 초과",
            description="딜러를 선택하지 않아 게임을 취소하고 베팅액을 반환했습니다.",
            color=0x95a5a6
        )
        self.clear_items()
        if self.message:
            try:
                await self.message.edit(embed=embed, view=None)
            except:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ 이 게임의 주인이 아닙니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="아리스 (Aris)", style=discord.ButtonStyle.blurple, emoji="🧹")
    async def select_aris(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.start_game(interaction, "aris")

    @discord.ui.button(label="케이 (Key)", style=discord.ButtonStyle.secondary, emoji="⚙️")
    async def select_key(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.start_game(interaction, "key")

    @discord.ui.button(label="게임 종료", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._ended = True
        try:
            update_balance(self.user.id, self.amount)
        finally:
            GLOBAL_PLAYING_USERS.discard(self.user.id)
            BLACKJACK_GAMES.pop(self.user.id, None)
        embed = discord.Embed(
            title="🛑 게임 취소",
            description="블랙잭 시뮬레이션을 취소했습니다. 베팅액이 반환되었습니다.",
            color=0x95a5a6
        )
        await interaction.response.edit_message(embed=embed, view=None)

    async def start_game(self, interaction: discord.Interaction, char_type):
        self._ended = True
        self.stop()
        deck         = make_deck(); random.shuffle(deck)
        player_cards = [deal_card(deck), deal_card(deck)]
        dealer_cards = [deal_card(deck), deal_card(deck)]

        p_score  = get_bj_score(player_cards)
        d_score  = get_bj_score(dealer_cards)
        dialogue = get_character_dialogue(char_type)
        game_view = BlackjackGameView(self.user, self.amount, deck, player_cards, dealer_cards, char_type)
        channel_id = interaction.channel.id if interaction.channel else None
        BLACKJACK_GAMES[self.user.id] = {
            "stage": "playing", "view": game_view,
            "bet": self.amount, "channel_id": channel_id,
        }

        d_show = f"{card_str(dealer_cards[0])} [??]"
        p_show = f"{card_str(player_cards[0])} {card_str(player_cards[1])} ({p_score})"

        player_bj = (p_score == 21 and len(player_cards) == 2)
        dealer_bj = (d_score == 21 and len(dealer_cards) == 2)

        if player_bj or dealer_bj:
            if player_bj and dealer_bj:   result = "push"
            elif dealer_bj:               result = "dealer_blackjack"
            else:                         result = "blackjack"
            color_map = {"push": 0x95a5a6, "dealer_blackjack": 0xFF4444, "blackjack": 0xFFD700}
            msg_map   = {
                "push":             dialogue["push"],
                "dealer_blackjack": dialogue["dealer_blackjack"],
                "blackjack":        dialogue["blackjack"],
            }
            try:
                if result == "blackjack":
                    update_balance(self.user.id, self.amount + int(self.amount * 1.5))
                elif result == "push":
                    update_balance(self.user.id, self.amount)
            finally:
                GLOBAL_PLAYING_USERS.discard(self.user.id)
                BLACKJACK_GAMES.pop(self.user.id, None)
            game_view._ended = True
            d_full = f"{card_str(dealer_cards[0])} {card_str(dealer_cards[1])} ({d_score})"
            embed2 = discord.Embed(title=dialogue["title"], description=msg_map[result], color=color_map[result])
            embed2.add_field(name=dialogue["dealer_name"], value=d_full, inline=False)
            embed2.add_field(name=f"👤 {self.user.display_name} 선생님", value=p_show, inline=False)
            embed2.set_footer(text=f"베팅액: {self.amount:,} 청휘석")
            await interaction.response.edit_message(embed=embed2, view=None)
            return

        # ── 딜러 A 공개 시 보험 제안 ────────────────────────────
        dealer_upcard_rank = dealer_cards[0] // 4 + 2
        if dealer_upcard_rank == 14:
            game_view.insurance_offered = True

        expiry = game_view._timeout_expiry
        desc   = dialogue["start"] + f"\n\n⏳ **남은 시간:** <t:{expiry}:R>"
        if game_view.insurance_offered:
            desc += "\n\n🛡️ **딜러 에이스 공개! 보험(Insurance)에 가입하시겠습니까?**"
        embed = discord.Embed(title=dialogue["title"], description=desc, color=dialogue["color"])
        embed.add_field(name=dialogue["dealer_name"], value=d_show, inline=False)
        embed.add_field(name=f"👤 {self.user.display_name} 선생님", value=p_show, inline=False)
        embed.set_footer(text=f"베팅액: {self.amount:,} 청휘석")

        game_view._sync_buttons()
        await interaction.response.edit_message(embed=embed, view=game_view)
        try:
            game_view.message = await interaction.original_response()
        except:
            pass


# ── 멀티핸드 구조 ────────────────────────────────
MAX_SPLITS = 4


class BJHand:
    """블랙잭 개별 핸드 (스플릿 시 여러 개 존재 가능)"""
    def __init__(self, cards, bet, is_split_ace=False, from_split=False):
        self.cards        = cards
        self.bet          = bet
        self.done         = False
        self.is_split_ace = is_split_ace
        self.from_split   = from_split
        self.result       = None   # "win"/"lose"/"push"/"blackjack"/"dealer_blackjack"
        self.doubled      = False

    def score(self):
        return get_bj_score(self.cards)

    def is_blackjack(self):
        return (not self.from_split) and len(self.cards) == 2 and self.score() == 21

    def is_bust(self):
        return self.score() > 21


class BlackjackGameView(discord.ui.View):
    def __init__(self, user, bet_amount, deck, player_cards, dealer_cards, char_type):
        super().__init__(timeout=60)
        self.user            = user
        self.deck            = deck
        self.d_cards         = dealer_cards
        self.char_type       = char_type
        self.dialogue        = get_character_dialogue(char_type)
        self._ended          = False
        self.has_seen_cards  = True
        self.message         = None
        self._timeout_expiry = int(time.time()) + 60

        self.hands        = [BJHand(player_cards, bet_amount)]
        self.cur_hand_idx = 0
        self.split_count  = 0

        self.insurance_offered  = False
        self.insurance_taken    = False
        self.insurance_resolved = False

        self._sync_buttons()

    @property
    def bet(self):
        return self.hands[0].bet if self.hands else 0

    @property
    def p_cards(self):
        return self.hands[self.cur_hand_idx].cards if self.hands else []

    def cur_hand(self):
        return self.hands[self.cur_hand_idx]

    def total_bet(self):
        return sum(h.bet for h in self.hands)

    async def on_timeout(self):
        if self._ended:
            return
        self._ended = True
        try:
            if self.has_seen_cards:
                result_key = "timeout_lose"
                color      = 0xFF0000
                for h in self.hands:
                    if h.result is None:
                        h.result = "lose"
                        h.done   = True
            else:
                update_balance(self.user.id, self.total_bet())
                result_key = "timeout_refund"
                color      = 0x95a5a6
        except Exception as e:
            print(f"[BJ Timeout Error] {e}")
            result_key = "timeout_lose"
            color = 0xFF0000
        finally:
            GLOBAL_PLAYING_USERS.discard(self.user.id)
            BLACKJACK_GAMES.pop(self.user.id, None)

        self.clear_items(); self.stop()
        try:
            embed = self.make_embed(end_game=True, result_msg=self.dialogue[result_key], color=color)
            if self.message:
                await self.message.edit(embed=embed, view=None)
        except Exception as e:
            print(f"[BJ Timeout Edit Error] {e}")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ 선생님의 차례가 아닙니다.", ephemeral=True)
            return False
        return True

    def _sync_buttons(self):
        if not self.hands:
            return
        h   = self.cur_hand()
        bal = get_user_balance(self.user.id)

        can_double = (len(h.cards) == 2 and not h.is_split_ace and bal >= h.bet)
        can_split  = (
            len(h.cards) == 2
            and not h.is_split_ace
            and self.split_count < MAX_SPLITS
            and bal >= h.bet
            and (h.cards[0] // 4 + 2) == (h.cards[1] // 4 + 2)
        )
        can_hit = not h.is_split_ace

        self.hit_btn.disabled       = not can_hit
        self.stand_btn.disabled     = False
        self.double_btn.disabled    = not can_double
        self.split_btn.disabled     = not can_split
        self.insurance_btn.disabled = not (self.insurance_offered and not self.insurance_resolved)

    def make_embed(self, end_game=False, result_msg="", color=None):
        tgt_color = color if color else self.dialogue["color"]
        d_score   = get_bj_score(self.d_cards)

        d_str = (" ".join(card_str(c) for c in self.d_cards) + f" ({d_score})") if end_game \
                else f"{card_str(self.d_cards[0])} [??]"

        desc = result_msg or self.dialogue["next"]
        if not end_game:
            desc += f"\n\n⏳ **남은 시간:** <t:{self._timeout_expiry}:R>"

        embed = discord.Embed(title=self.dialogue["title"], description=desc, color=tgt_color)
        embed.add_field(name=self.dialogue["dealer_name"], value=d_str, inline=False)

        if len(self.hands) == 1:
            h     = self.hands[0]
            p_str = " ".join(card_str(c) for c in h.cards) + f" ({h.score()})"
            embed.add_field(name=f"👤 {self.user.display_name} 선생님", value=p_str, inline=False)
        else:
            for i, h in enumerate(self.hands):
                marker = "▶️ " if (not end_game and i == self.cur_hand_idx) else ""
                tag = ""
                if end_game and h.result:
                    tag_map = {
                        "win": " ✅승", "lose": " ❌패", "push": " 🤝무",
                        "blackjack": " ✨BJ", "dealer_blackjack": " ❌패",
                    }
                    tag = tag_map.get(h.result, "")
                p_str = " ".join(card_str(c) for c in h.cards) + f" ({h.score()}){tag}"
                embed.add_field(name=f"{marker}핸드 {i+1} (베팅 {h.bet:,})", value=p_str, inline=False)

        footer = f"총 베팅: {self.total_bet():,} 청휘석"
        if self.insurance_taken:
            footer += " | 🛡️ 보험 가입됨"
        embed.set_footer(text=footer)
        return embed

    async def update_board(self, interaction, end_game=False, result_msg="", color=None):
        self._sync_buttons()
        embed = self.make_embed(end_game=end_game, result_msg=result_msg, color=color)
        if end_game:
            self._ended = True
            GLOBAL_PLAYING_USERS.discard(self.user.id)
            BLACKJACK_GAMES.pop(self.user.id, None)
            self.clear_items(); self.stop()
        await interaction.response.edit_message(embed=embed, view=self)
        try:
            self.message = await interaction.original_response()
        except:
            pass

    def _resolve_hand_vs_dealer(self, h, dealer_bj):
        if h.result is not None:
            return
        p_score = h.score()
        if dealer_bj and h.is_blackjack():
            h.result = "push"
        elif dealer_bj:
            h.result = "dealer_blackjack"
        elif h.is_blackjack():
            h.result = "blackjack"
        elif p_score > 21:
            h.result = "lose"
        else:
            d_score = get_bj_score(self.d_cards)
            if d_score > 21 or p_score > d_score:   h.result = "win"
            elif p_score == d_score:                 h.result = "push"
            else:                                    h.result = "lose"

    def _payout_hand(self, h):
        if h.result == "blackjack":
            update_balance(self.user.id, h.bet + int(h.bet * 1.5))
        elif h.result == "win":
            update_balance(self.user.id, h.bet * 2)
        elif h.result == "push":
            update_balance(self.user.id, h.bet)

    async def _finish_all_hands(self, interaction):
        any_live = any(not h.is_bust() for h in self.hands)
        if any_live:
            while get_bj_score(self.d_cards) < 17:
                self.d_cards.append(deal_card(self.deck))

        dealer_bj = (get_bj_score(self.d_cards) == 21 and len(self.d_cards) == 2)

        for h in self.hands:
            if h.is_bust():
                h.result = "lose"
            else:
                self._resolve_hand_vs_dealer(h, dealer_bj)

        for h in self.hands:
            self._payout_hand(h)

        if len(self.hands) == 1:
            r = self.hands[0].result
            color_map = {
                "blackjack": 0xFFD700, "win": 0x00FF00, "push": 0x95a5a6,
                "lose": 0xFF0000, "dealer_blackjack": 0xFF4444,
            }
            msg   = self.dialogue.get(r, self.dialogue["lose"])
            color = color_map.get(r, 0xFF0000)
        else:
            wins   = sum(1 for h in self.hands if h.result in ("win", "blackjack"))
            loses  = sum(1 for h in self.hands if h.result in ("lose", "dealer_blackjack"))
            pushes = sum(1 for h in self.hands if h.result == "push")
            if wins > loses:
                msg = self.dialogue["win"];  color = 0x00FF00
            elif loses > wins:
                msg = self.dialogue["lose"]; color = 0xFF0000
            else:
                msg = self.dialogue["push"]; color = 0x95a5a6
            msg += f"\n\n📊 스플릿 결과: 승 {wins} / 패 {loses} / 무 {pushes}"

        await self.update_board(interaction, end_game=True, result_msg=msg, color=color)

    async def _advance_or_finish(self, interaction):
        self.cur_hand().done = True
        next_idx = next((i for i, h in enumerate(self.hands) if not h.done), None)
        if next_idx is None:
            await self._finish_all_hands(interaction)
            return
        self.cur_hand_idx = next_idx
        h = self.cur_hand()
        if h.is_split_ace:
            h.cards.append(deal_card(self.deck))
            h.done = True
            await self._advance_or_finish(interaction)
            return
        await self.update_board(
            interaction,
            result_msg=f"➡️ **핸드 {self.cur_hand_idx + 1}** 진행합니다.\n\n" + self.dialogue["next"]
        )

    @discord.ui.button(label="히트 (Hit)", style=discord.ButtonStyle.primary, emoji="👊")
    async def hit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        h = self.cur_hand()
        h.cards.append(deal_card(self.deck))
        if h.score() >= 21:
            await self._advance_or_finish(interaction)
        else:
            await self.update_board(interaction, result_msg=self.dialogue["hit"])

    @discord.ui.button(label="스탠드 (Stand)", style=discord.ButtonStyle.success, emoji="✋")
    async def stand_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._advance_or_finish(interaction)

    @discord.ui.button(label="더블 다운 (Double)", style=discord.ButtonStyle.secondary, emoji="💰")
    async def double_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        h = self.cur_hand()
        if len(h.cards) != 2 or h.is_split_ace:
            return await interaction.response.send_message(
                "❌ 더블 다운은 (스플릿 에이스 제외) 첫 턴에만 가능합니다!", ephemeral=True
            )
        bal = get_user_balance(self.user.id)
        if bal < h.bet:
            return await interaction.response.send_message("❌ 소지금이 부족합니다!", ephemeral=True)
        update_balance(self.user.id, -h.bet)
        h.bet    *= 2
        h.doubled = True
        h.cards.append(deal_card(self.deck))
        await self._advance_or_finish(interaction)

    @discord.ui.button(label="스플릿 (Split)", style=discord.ButtonStyle.blurple, emoji="✂️")
    async def split_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        h = self.cur_hand()
        if len(h.cards) != 2 or h.is_split_ace:
            return await interaction.response.send_message(
                "❌ 스플릿은 최초 2장이 같은 값일 때만 가능합니다.", ephemeral=True
            )
        if (h.cards[0] // 4 + 2) != (h.cards[1] // 4 + 2):
            return await interaction.response.send_message(
                "❌ 두 카드의 값이 같아야 스플릿할 수 있습니다.", ephemeral=True
            )
        if self.split_count >= MAX_SPLITS:
            return await interaction.response.send_message(
                f"❌ 최대 스플릿 횟수({MAX_SPLITS}회)를 초과했습니다.", ephemeral=True
            )
        bal = get_user_balance(self.user.id)
        if bal < h.bet:
            return await interaction.response.send_message("❌ 소지금이 부족합니다!", ephemeral=True)

        update_balance(self.user.id, -h.bet)
        self.split_count += 1

        is_ace = (h.cards[0] // 4 + 2) == 14
        card1, card2 = h.cards[0], h.cards[1]
        new_bet = h.bet

        hand1 = BJHand([card1, deal_card(self.deck)], new_bet, is_split_ace=is_ace, from_split=True)
        hand2 = BJHand([card2, deal_card(self.deck)], new_bet, is_split_ace=is_ace, from_split=True)
        self.hands[self.cur_hand_idx:self.cur_hand_idx + 1] = [hand1, hand2]

        if is_ace:
            hand1.done = True
            hand2.done = True
            next_idx = next((i for i, hh in enumerate(self.hands) if not hh.done), None)
            if next_idx is None:
                await self._finish_all_hands(interaction)
            else:
                self.cur_hand_idx = next_idx
                await self.update_board(
                    interaction,
                    result_msg=f"✂️ **A 스플릿!** 각 핸드에 카드 1장씩 지급됩니다.\n\n"
                               f"➡️ **핸드 {self.cur_hand_idx + 1}** 진행합니다.\n\n" + self.dialogue["next"]
                )
            return

        await self.update_board(
            interaction,
            result_msg="✂️ **스플릿!** 핸드가 2개로 나뉘었습니다.\n\n핸드 1부터 진행합니다.\n\n" + self.dialogue["next"]
        )

    @discord.ui.button(label="보험 (Insurance)", style=discord.ButtonStyle.danger, emoji="🛡️", row=1)
    async def insurance_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.insurance_offered or self.insurance_resolved:
            return await interaction.response.send_message("❌ 지금은 보험에 가입할 수 없습니다.", ephemeral=True)
        base_bet = self.hands[0].bet
        ins_cost = base_bet // 2
        bal = get_user_balance(self.user.id)
        if bal < ins_cost:
            return await interaction.response.send_message("❌ 보험금이 부족합니다.", ephemeral=True)

        update_balance(self.user.id, -ins_cost)
        self.insurance_taken    = True
        self.insurance_resolved = True

        dealer_bj = (get_bj_score(self.d_cards) == 21 and len(self.d_cards) == 2)
        if dealer_bj:
            payout = ins_cost * 3
            update_balance(self.user.id, payout)
            note = f"🛡️ 보험 적중! 딜러 블랙잭 확인 → +{payout:,} 청휘석 지급"
        else:
            note = "🛡️ 보험에 가입했지만 딜러는 블랙잭이 아니었습니다. 보험금은 소멸합니다."

        self._sync_buttons()
        await self.update_board(interaction, result_msg=f"{note}\n\n" + self.dialogue["next"])
# ==========================================
# 🖥️ 포커 UI
# ==========================================
class AdminControlView(discord.ui.View):
    def __init__(self, table, parent_view):
        super().__init__(timeout=None)
        self.table       = table
        self.parent_view = parent_view

        if self.table.players:
            options = [
                discord.SelectOption(
                    label=p.member.display_name,
                    value=str(p.member.id),
                    description="이 학생을 대기실에서 내보냅니다."
                ) for p in self.table.players
            ]
            self.kick_select = discord.ui.Select(
                placeholder="🚨 강퇴할 학생을 선택하세요...",
                min_values=1, max_values=1, options=options[:25]
            )
            self.kick_select.callback = self.kick_callback
            self.add_item(self.kick_select)

    async def kick_callback(self, interaction: discord.Interaction):
        if not is_host_or_admin(interaction, self.table):
            return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
        target_id = int(self.kick_select.values[0])
        if target_id == self.table.host.id:
            return await interaction.response.send_message("❌ 담당 선생님(방장)은 내보낼 수 없습니다.", ephemeral=True)
        await self.table.kick_player(target_id)
        await interaction.response.send_message("✅ 해당 학생을 작전에서 제외하고 크레딧을 반환했습니다.", ephemeral=True)
        await self.parent_view.update_lobby_message(interaction)

    @discord.ui.button(label="💥 작전 방 강제 종료 (폭파)", style=discord.ButtonStyle.danger, row=1)
    async def destroy_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_host_or_admin(interaction, self.table):
            return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
        result = await self.table.force_finish_table()
        embed = discord.Embed(
            title="💥 테이블 종료됨",
            description="관리자 권한으로 테이블이 종료되었습니다.\n"
                        + ("진행 중인 게임은 현재 상태 기준으로 정산되었습니다."
                           if result == "finished"
                           else "대기 중인 테이블은 취소되고 참가비가 반환되었습니다."),
            color=0xFF0000
        )
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="돌아가기", style=discord.ButtonStyle.secondary, row=1)
    async def go_back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.parent_view.update_lobby_message(interaction)


class PokerLobbyView(discord.ui.View):
    def __init__(self, table):
        super().__init__(timeout=None)
        self.table = table

    def _make_lobby_embed(self):
        g_name  = "텍사스 홀덤" if self.table.game_type == "holdem" else "세븐 포커"
        bs_rule = "🔥 적용 (마운틴 다음)" if self.table.use_back_straight else "일반 룰 (최하위)"
        embed = discord.Embed(
            title=f"🃏 {g_name} 대기방",
            description=f"방장: {self.table.host.mention}",
            color=0x3498db
        )
        embed.add_field(name="참가비",       value=f"{self.table.entry_cost:,} 청휘석", inline=True)
        embed.add_field(name="현재 인원",    value=f"{len(self.table.players)}/6 명",   inline=True)
        embed.add_field(name="백 스트레이트", value=bs_rule,                              inline=True)
        embed.add_field(
            name="참가자 목록",
            value="\n".join(f"👤 {p.member.display_name}" for p in self.table.players) or "없음",
            inline=False
        )
        sched_lines  = []
        bb           = self.table.bb
        sb           = self.table.sb
        interval_min = self.table.blind_interval // 60
        for i in range(5):
            mins = i * interval_min
            sched_lines.append(f"Lv.{i+1} ({mins}분~): SB {sb*(2**i):,} / BB {bb*(2**i):,}")
        embed.add_field(name="📈 블라인드 스케줄 (예정)", value="\n".join(sched_lines), inline=False)
        return embed

    async def update_lobby_message(self, interaction):
        if self.table.is_destroyed: return
        embed = self._make_lobby_embed()
        if interaction.response.is_done():
            await interaction.message.edit(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="참가", style=discord.ButtonStyle.primary, custom_id="join_btn", row=0)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, msg = await self.table.add_player_to_table(interaction.user)
        if not ok:
            return await interaction.response.send_message(msg, ephemeral=True)
        if self.table.game_started:
            return await interaction.response.send_message(msg, ephemeral=True)
        await self.update_lobby_message(interaction)

    @discord.ui.button(label="퇴장", style=discord.ButtonStyle.secondary, custom_id="leave_btn", row=0)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self.table.get_player(interaction.user.id)
        if not p:
            return await interaction.response.send_message("❌ 참가 중이 아닙니다.", ephemeral=True)
        if self.table.game_started:
            ok, msg = await self.table.abort_player(interaction.user)
            return await interaction.response.send_message(msg, ephemeral=True)

        self.table.players.remove(p)
        GLOBAL_PLAYING_USERS.discard(interaction.user.id)
        if self.table.entry_cost > 0:
            update_balance(interaction.user.id, self.table.entry_cost)

        if not self.table.players:
            if self.table.table_id in bot.tables:
                del bot.tables[self.table.table_id]
            self.clear_items()
            embed = discord.Embed(title="🗑️ 방이 삭제되었습니다. (인원 0명)", color=0x555555)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            if self.table.host.id == interaction.user.id:
                self.table.host = self.table.players[0].member
            await self.update_lobby_message(interaction)

    @discord.ui.button(label="시작", style=discord.ButtonStyle.success, custom_id="start_btn", row=0)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_host_or_admin(interaction, self.table):
            return await interaction.response.send_message("❌ 방장 또는 관리자만 시작할 수 있습니다.", ephemeral=True)
        if len(self.table.players) < 2:
            return await interaction.response.send_message("❌ 최소 2명이 필요합니다.", ephemeral=True)
        if self.table.game_started:
            return await interaction.response.send_message("❌ 이미 시작되었습니다.", ephemeral=True)

        self.clear_items()
        embed = discord.Embed(
            title="🚀 작전 개시!",
            description="샬레의 모든 자원을 투입합니다. 선생님, 행운을 빕니다!",
            color=0x00ff00
        )
        await interaction.response.edit_message(embed=embed, view=self)
        await self.table.start_tournament()

    @discord.ui.button(label="🛡️ 관리자 메뉴", style=discord.ButtonStyle.secondary, custom_id="admin_btn", row=1)
    async def open_admin_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_host_or_admin(interaction, self.table):
            return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
        if self.table.game_started:
            return await interaction.response.send_message("❌ 게임이 이미 시작되어 설정 메뉴를 열 수 없습니다.", ephemeral=True)
        embed = discord.Embed(
            title="🛡️ 작전 제어판",
            description="특정 학생을 내보내거나 작전을 강제 취소할 수 있습니다.",
            color=0x808080
        )
        await interaction.response.edit_message(embed=embed, view=AdminControlView(self.table, self))


async def send_private_hand_info(interaction, table):
    p = table.get_player(interaction.user.id)
    if not p or not p.hole_cards:
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ 핸드가 없습니다.", ephemeral=True)
        return
    score, _, best5 = eval_hand(p.hole_cards, table.use_back_straight)
    embed = discord.Embed(title="🕵️ 내 정보 확인", color=0x2f3136)

    if table.game_type == "seven":
        hidden_str = " ".join(card_str(c) for c in p.cards_hidden)
        open_str   = " ".join(card_str(c) for c in p.cards_open)
        embed.add_field(name="내 패 (🔒 숨김 | 👁️ 공개)", value=f"🔒 {hidden_str}  |  👁️ {open_str}", inline=False)
    else:
        embed.add_field(name="내 패", value=f"# {card_str(p.hole_cards[0])} {card_str(p.hole_cards[1])}", inline=False)

    embed.add_field(name="현재 족보", value=f"### {hand_name(score)}", inline=False)
    embed.add_field(name="구성 카드", value=f"**{' '.join(card_str(c) for c in best5)}**", inline=False)

    if table.game_type == "holdem" and table.community:
        combined = p.hole_cards + table.community
        c_score, _, c_best5 = eval_hand(combined, table.use_back_straight)
        embed.add_field(
            name="🃏 현재 최강 족보 (보드 포함)",
            value=f"**{hand_name(c_score)}** — {' '.join(card_str(c) for c in c_best5)}",
            inline=False
        )

    if not interaction.response.is_done():
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.followup.send(embed=embed, ephemeral=True)


class ShowHandView(discord.ui.View):
    def __init__(self, table):
        super().__init__(timeout=None)
        self.table = table

    @discord.ui.button(label="👀 내 패 & 족보 (나만 보임)", style=discord.ButtonStyle.blurple, custom_id="check_hand")
    async def check_hand(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_private_hand_info(interaction, self.table)


class RaiseModal(discord.ui.Modal, title="레이즈"):
    amount = discord.ui.TextInput(label="추가 베팅액", placeholder="숫자만 입력")

    def __init__(self, table, player):
        super().__init__()
        self.table  = table
        self.player = player

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(self.amount.value)
            if val <= 0: raise ValueError
            await self.table.process_action(interaction, "raise", val)
        except:
            await interaction.response.send_message("❌ 올바른 숫자를 입력하세요.", ephemeral=True)


class RebuyView(discord.ui.View):
    def __init__(self, table):
        super().__init__(timeout=None)
        self.table = table

    @discord.ui.button(label="💰 리바이인", style=discord.ButtonStyle.green)
    async def rebuy(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.table.rebuy_period:
            return await interaction.response.send_message("⏳ 리바이인 기간이 마감되었습니다.", ephemeral=True)
        p = self.table.get_player(interaction.user.id)
        if not p or p.chips > 0:
            return await interaction.response.send_message("❌ 리바이인 대상이 아닙니다.", ephemeral=True)
        stack = self.table.starting_stack()
        if stack < self.table.bb * 25:
            return await interaction.response.send_message("❌ 블라인드가 너무 높아 리바이인이 불가합니다.", ephemeral=True)
        if self.table.entry_cost > 0:
            bal = get_user_balance(interaction.user.id)
            if bal < self.table.entry_cost:
                return await interaction.response.send_message("❌ 잔액이 부족합니다.", ephemeral=True)
            update_balance(interaction.user.id, -self.table.entry_cost)
            self.table.prize_pool += self.table.entry_cost
        p.chips   = stack
        p.is_bust = False
        p.rank    = None
        if p in self.table.eliminated_players:
            self.table.eliminated_players.remove(p)
        await interaction.response.send_message("✅ 리바이인 완료! 전력을 재정비했습니다.", ephemeral=True)
        await self.table.channel.send(
            f"🔄 **{p.member.display_name}** 리바이인! (총 상금 풀: {self.table.prize_pool:,} 청휘석)"
        )


class ActionView(discord.ui.View):
    def __init__(self, table):
        super().__init__(timeout=None)
        self.table = table

    async def check_turn_or_preaction(self, interaction, action_type):
        p = self.table.get_player(interaction.user.id)
        if not p:
            await interaction.response.send_message("❌ 참가자가 아닙니다.", ephemeral=True)
            return False
        current_p = self.table.players[self.table.current_player_idx]
        if current_p == p:
            return True
        if p.folded or p.is_bust or p.all_in:
            await interaction.response.send_message("❌ 예약할 수 없는 상태입니다.", ephemeral=True)
            return False
        if p.acted:
            await interaction.response.send_message("❌ 이미 행동을 완료했습니다.", ephemeral=True)
            return False
        if action_type in ("raise", "allin"):
            await interaction.response.send_message("❌ 예약은 Check/Call/Fold만 가능합니다.", ephemeral=True)
            return False
        if p.pre_action == action_type:
            p.pre_action = None
            await interaction.response.send_message("✅ 예약이 취소되었습니다.", ephemeral=True)
        else:
            p.pre_action = action_type
            label = "Check/Fold" if action_type == "fold" else "Auto Call/Check"
            await interaction.response.send_message(f"✅ **{label}** 예약됨!", ephemeral=True)
        return False

    @discord.ui.button(label="👀 내 패 & 족보", style=discord.ButtonStyle.blurple, row=0)
    async def check_hand_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_private_hand_info(interaction, self.table)

    @discord.ui.button(label="Fold", style=discord.ButtonStyle.danger, row=1)
    async def fold(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self.check_turn_or_preaction(interaction, "fold"):
            await self.table.process_action(interaction, "fold")

    @discord.ui.button(label="Check/Call", style=discord.ButtonStyle.secondary, row=1)
    async def call(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self.check_turn_or_preaction(interaction, "call"):
            p   = self.table.players[self.table.current_player_idx]
            act = "check" if self.table.current_bet == p.bet else "call"
            await self.table.process_action(interaction, act)

    @discord.ui.button(label="Raise", style=discord.ButtonStyle.success, row=1)
    async def raise_bet(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self.check_turn_or_preaction(interaction, "raise"):
            p = self.table.players[self.table.current_player_idx]
            await interaction.response.send_modal(RaiseModal(self.table, p))

    @discord.ui.button(label="All-in", style=discord.ButtonStyle.primary, row=1)
    async def allin(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self.check_turn_or_preaction(interaction, "allin"):
            await self.table.process_action(interaction, "allin")

# ==========================================
# 🎮 Game Data Classes
# ==========================================
class Player:
    def __init__(self, member, chips):
        self.member       = member
        self.chips        = chips
        self.bet          = 0
        self.total_wager  = 0
        self.folded       = False
        self.all_in       = False
        self.acted        = False
        self.is_bust      = False
        self.hole_cards   = []
        self.rank         = None
        self.pre_action   = None
        self.cards_open   = []
        self.cards_hidden = []
        self.join_pending = False

    def reset_round(self):
        self.bet   = 0
        self.acted = False

    def reset_hand(self):
        self.bet          = 0
        self.total_wager  = 0
        self.folded       = False
        self.all_in       = False
        self.acted        = False
        self.hole_cards   = []
        self.pre_action   = None
        self.cards_open   = []
        self.cards_hidden = []
        self.join_pending = False


class Table:
    def __init__(self, channel, entry_cost, host, table_id,
                 game_type="holdem", use_back_straight=False,
                 timeout_seconds=30, blind_interval=900, blind_multiplier=2):
        self.channel           = channel
        self.entry_cost        = entry_cost
        self.host              = host
        self.table_id          = table_id
        self.game_type         = game_type
        self.use_back_straight = use_back_straight
        self.timeout_seconds   = timeout_seconds
        self.blind_interval    = blind_interval
        self.blind_multiplier  = blind_multiplier

        self.prize_pool         = 0
        self.players            = []
        self.eliminated_players = []
        self.bb                 = 100
        self.sb                 = 50
        self.deck               = []
        self.community          = []
        self.pot                = 0
        self.dealer_idx         = 0
        self.game_started       = False
        self.hand_active        = False
        self.blind_timer_task   = None
        self.turn_timer_task    = None
        self.action_msg         = None
        self.current_player_idx = 0
        self.betting_round      = 0
        self.current_bet        = 0
        self.min_raise          = self.bb
        self.rebuy_period       = False
        self.turn_start_time    = 0
        self.is_destroyed       = False
        self.log_channel        = None
        self.aborted_players    = {}

    def starting_stack(self):
        return self.entry_cost if self.entry_cost > 0 else 10000

    def get_player(self, mid):
        return next((p for p in self.players if p.member.id == mid), None)

    def active_players(self):
        return [p for p in self.players if not p.folded and not p.is_bust and not p.join_pending]

    def survivor_players(self):
        return [p for p in self.players if not p.is_bust]

    def players_can_act(self):
        return [p for p in self.active_players() if not p.all_in]

    async def add_player_to_table(self, member):
        if self.get_player(member.id):
            return False, "❌ 이미 이 테이블에 참가 중입니다."
        if len(self.players) >= 6:
            return False, "❌ 테이블이 꽉 찼습니다."
        if member.id in GLOBAL_PLAYING_USERS:
            return False, "❌ 이미 다른 게임에 참가 중입니다."

        record = self.aborted_players.get(member.id)
        if record and record.get("valid") and self.game_started:
            stack = max(0, record["chips"])
            del self.aborted_players[member.id]
            player = Player(member, stack)
            player.join_pending = self.hand_active
            if player.join_pending:
                player.folded = True
                player.acted  = True
            self.players.append(player)
            GLOBAL_PLAYING_USERS.add(member.id)
            await self.broadcast(
                content=f"🔁 **{member.display_name}** 플레이어가 이전 청휘석 {stack:,}으로 복귀했습니다."
            )
            return True, f"✅ 이전 청휘석 **{stack:,}**으로 같은 테이블에 복귀했습니다."

        if member.id in self.aborted_players:
            del self.aborted_players[member.id]

        bal = get_user_balance(member.id)
        if self.entry_cost > 0 and bal < self.entry_cost:
            return False, "❌ 잔액이 부족합니다."
        if self.entry_cost > 0:
            update_balance(member.id, -self.entry_cost)
            if self.game_started:
                self.prize_pool += self.entry_cost

        player = Player(member, self.starting_stack())
        player.join_pending = self.game_started and self.hand_active
        if player.join_pending:
            player.folded = True
            player.acted  = True
        self.players.append(player)
        GLOBAL_PLAYING_USERS.add(member.id)
        if self.game_started:
            await self.broadcast(
                content=f"➕ **{member.display_name}** 플레이어가 테이블에 참가했습니다. 다음 핸드부터 행동합니다."
            )
        return True, "✅ 테이블에 참가했습니다."

    async def abort_player(self, member):
        """
        게임 중 자발적 이탈.
        현재 칩 수를 기록해 두고 나가며, 같은 테이블 경기가 끝나지 않은 동안은
        재참가 시 그 칩으로 복귀할 수 있다.
        """
        p = self.get_player(member.id)
        if not p:
            return False, "❌ 참가 중이 아닙니다."

        saved_chips = max(0, p.chips)
        if p.bet > 0:
            saved_chips      += p.bet
            self.pot          = max(0, self.pot - p.bet)

        self.aborted_players[member.id] = {
            "member": member,
            "chips":  saved_chips,
            "valid":  True,
        }

        # ── [버그픽스 문제4] was_current는 반드시 remove 전에 판단 ──
        was_current = (
            self.hand_active
            and len(self.players) > 0
            and self.players[self.current_player_idx % len(self.players)] == p
        )

        if p in self.players:
            self.players.remove(p)
        GLOBAL_PLAYING_USERS.discard(member.id)

        # ── [버그픽스 문제4] remove 후 인덱스 즉시 보정 ────────────
        if len(self.players) > 0:
            self.current_player_idx %= len(self.players)
        else:
            self.current_player_idx = 0

        await self.broadcast(
            content=f"🏳️ **{member.display_name}** 플레이어가 현재 청휘석 {saved_chips:,}을 기록하고 테이블을 중단했습니다."
                    "\n(같은 테이블에 재참가하면 해당 청휘석으로 복귀합니다.)"
        )

        if not self.players:
            self.is_destroyed = True
            self.cancel_turn_timer()
            if self.blind_timer_task:
                self.blind_timer_task.cancel()
            for rec in self.aborted_players.values():
                rec["valid"] = False
            if self.table_id in bot.tables:
                del bot.tables[self.table_id]
            return True, (
                f"✅ 현재 청휘석 **{saved_chips:,}**을 기록하고 중단했습니다. "
                "남은 플레이어가 없어 테이블이 종료되었습니다."
            )

        if self.host.id == member.id:
            self.host = self.players[0].member

        if self.hand_active:
            active = self.active_players()

            # ── [버그픽스 문제2] join_pending 제외 실질 생존자 1명 이하 체크 ──
            non_pending_survivors = [p for p in self.survivor_players() if not p.join_pending]
            if len(non_pending_survivors) <= 1:
                self.cancel_turn_timer()
                if non_pending_survivors:
                    await self.end_hand_premature()
                else:
                    await self.finalize_hand()
                return True, f"✅ 현재 청휘석 **{saved_chips:,}**을 기록하고 테이블 참가를 중단했습니다."

            if len(active) <= 1:
                self.cancel_turn_timer()
                if active:
                    await self.end_hand_premature()
                else:
                    await self.finalize_hand()
            elif was_current:
                self.cancel_turn_timer()
                await self.next_turn_or_street()

        # hand_active=False 상태 생존자 1명 이하 체크
        survivors = self.survivor_players()
        if self.game_started and not self.hand_active and len(survivors) <= 1:
            await self.end_tournament(survivors[0] if survivors else None)

        return True, f"✅ 현재 청휘석 **{saved_chips:,}**을 기록하고 테이블 참가를 중단했습니다."

    def _invalidate_aborted_records(self):
        for rec in self.aborted_players.values():
            rec["valid"] = False

    def dealer_order(self, candidates):
        if not candidates:
            return []
        survivors = self.survivor_players()
        if not survivors:
            return list(candidates)
        dealer = survivors[self.dealer_idx % len(survivors)]
        try:
            dealer_pos = self.players.index(dealer)
        except ValueError:
            dealer_pos = 0

        def order_key(player):
            try:
                return (self.players.index(player) - dealer_pos - 1) % len(self.players)
            except ValueError:
                return len(self.players)

        return sorted(candidates, key=order_key)

    async def broadcast(self, content=None, embed=None, view=None):
        try:
            return await self.channel.send(content=content, embed=embed, view=view)
        except Exception as e:
            print(f"[Broadcast Error] {e}")

    async def log(self, content):
        if self.log_channel:
            try:
                await self.log_channel.send(content)
            except:
                pass

    async def kick_player(self, user_id: int):
        p = self.get_player(user_id)
        if p:
            self.players.remove(p)
            GLOBAL_PLAYING_USERS.discard(user_id)
            if self.entry_cost > 0:
                update_balance(user_id, self.entry_cost)

    async def destroy_table(self):
        self.is_destroyed = True
        self.cancel_turn_timer()
        if self.blind_timer_task:
            self.blind_timer_task.cancel()
        for p in self.players:
            GLOBAL_PLAYING_USERS.discard(p.member.id)
            if self.entry_cost > 0:
                update_balance(p.member.id, self.entry_cost)
        self.players.clear()

    async def force_finish_table(self):
        """
        관리자 강제 종료.
        대기 중이면 취소 + 참가비 반환.
        진행 중이면 현재 칩 기준으로 정산.
        """
        if not self.game_started:
            await self.destroy_table()
            if self.table_id in bot.tables:
                del bot.tables[self.table_id]
            return "cancelled"

        # ── [버그픽스 문제3] 타이머 정리 후 hand_active 명시적으로 내림 ──
        self.cancel_turn_timer()
        if self.blind_timer_task:
            self.blind_timer_task.cancel()
            self.blind_timer_task = None
        self.hand_active = False   # finalize_hand 재진입 방지

        candidates = self.survivor_players() or self.players
        winner     = max(candidates, key=lambda p: p.chips, default=None)
        await self.broadcast(content="🛑 **관리자 권한으로 현재 상태 기준 게임을 종료합니다.**")
        await self.end_tournament(winner)
        return "finished"

    def get_position_name(self, player_idx):
        total  = len(self.players)
        if total < 2: return "?"
        offset = (player_idx - self.dealer_idx) % total
        if total == 2:
            return "SB" if offset == 1 else "BB (BTN)"
        if total == 3:
            return {0: "BTN", 1: "SB", 2: "BB"}.get(offset, "?")
        return "BTN" if offset == 0 else ("SB" if offset == 1 else ("BB" if offset == 2 else f"Pos{offset}"))

    def get_table_status_str(self):
        lines = []
        for i, p in enumerate(self.players):
            pos_name = self.get_position_name(i)
            if   p.folded:     status = "🚫 Fold"
            elif p.is_bust:    status = "💀 Bust"
            elif p.all_in:     status = "🔥 All-in"
            elif p.pre_action: status = "⚡ Reserved"
            else:              status = ""

            cards_display = ""
            if self.game_type == "seven" and not p.is_bust:
                open_str      = " ".join(card_str(c) for c in p.cards_open)
                hidden_str    = " ".join("[??]" for _ in p.cards_hidden)
                cards_display = f" | 공개: {open_str} {hidden_str}"

            pointer = "▶️" if i == self.current_player_idx else "  "
            lines.append(f"{pointer} `{pos_name}` **{p.member.display_name}**: {p.chips:,} {status}{cards_display}")
        return "\n".join(lines)

    async def start_blind_timer(self):
        self.blind_timer_task = asyncio.create_task(self._blind_loop())

    async def _blind_loop(self):
        while self.game_started and not self.is_destroyed:
            await asyncio.sleep(self.blind_interval)
            self.bb = int(self.bb * self.blind_multiplier)
            self.sb = self.bb // 2
            msg = f"🆙 **블라인드 상승!** SB: {self.sb:,} / BB: {self.bb:,}"
            await self.broadcast(content=msg)
            await self.log(msg)

    async def start_turn_timer(self):
        self.cancel_turn_timer()
        self.turn_start_time  = time.time()
        self.turn_timer_task  = asyncio.create_task(self._turn_loop())

    def cancel_turn_timer(self):
        if self.turn_timer_task:
            self.turn_timer_task.cancel()
            self.turn_timer_task = None

    async def _turn_loop(self):
        try:
            await asyncio.sleep(self.timeout_seconds)
            if self.hand_active and not self.is_destroyed:
                await self.force_timeout_action()
        except asyncio.CancelledError:
            pass

    async def force_timeout_action(self):
        if not self.hand_active: return
        p           = self.players[self.current_player_idx]
        call_amt    = self.current_bet - p.bet
        action_type = "check" if call_amt == 0 else "fold"
        msg = f"⏰ **{p.member.display_name}** 응답 없음 → 자동 **{action_type.upper()}**"
        await self.broadcast(content=msg)
        await self.log(msg)
        self.cancel_turn_timer()
        await self.process_action(None, action_type)

    async def return_uncalled_bets(self):
        active_bets = [p.bet for p in self.active_players()]
        if not active_bets: return
        sorted_bets = sorted(active_bets, reverse=True)
        if len(sorted_bets) < 2: return
        if sorted_bets[0] > sorted_bets[1]:
            high = sorted_bets[0]
            sec  = sorted_bets[1]
            for p in self.active_players():
                if p.bet == high:
                    ref            = high - sec
                    p.chips       += ref
                    p.bet         -= ref
                    p.total_wager -= ref
                    self.pot      -= ref
                    msg = f"💸 **초과 베팅 반환:** {p.member.display_name} +{ref:,} 칩"
                    await self.broadcast(content=msg)
                    break

    async def start_tournament(self):
        if self.game_started: return
        self.game_started = True
        self.prize_pool   = len(self.players) * self.entry_cost
        g_title = "텍사스 홀덤" if self.game_type == "holdem" else "세븐 포커"

        embed = discord.Embed(
            title=f"🏁 [{g_title}] 작전 개시!",
            description=f"총 상금: **{self.prize_pool:,} 청휘석**\n"
                        f"턴 제한: **{self.timeout_seconds}초** | "
                        f"블라인드 업: **{self.blind_interval//60}분** 마다 x{self.blind_multiplier}",
            color=0x00ff00
        )
        await self.broadcast(embed=embed)
        await self.log(f"[게임 시작] {g_title} | {len(self.players)}명 | 상금 {self.prize_pool:,}")
        await self.start_blind_timer()
        await self.start_hand()

    def set_first_actor_seven(self):
        active = self.active_players()
        if not active: return

        def open_strength(player):
            if not player.cards_open: return (-1, -1)
            score, kickers, _ = eval_hand(player.cards_open, self.use_back_straight)
            return (score, kickers)

        active.sort(key=open_strength, reverse=True)
        self.current_player_idx = self.players.index(active[0])

    async def start_hand(self):
        if self.is_destroyed: return
        survivors = self.survivor_players()
        if len(survivors) == 1:
            return await self.end_tournament(survivors[0])

        self.deck          = make_deck(); random.shuffle(self.deck)
        self.community     = []
        self.pot           = 0
        self.betting_round = 0
        self.hand_active   = True
        self.min_raise     = self.bb

        if self.game_type == "holdem":
            for p in self.players:
                p.reset_hand()
                if not p.is_bust:
                    p.hole_cards = [deal_card(self.deck), deal_card(self.deck)]

            count      = len(survivors)
            dealer_ptr = self.dealer_idx % count
            sb_p = survivors[(dealer_ptr + 1) % count]
            bb_p = survivors[(dealer_ptr + 2) % count]

            for player, amt in ((sb_p, self.sb), (bb_p, self.bb)):
                pay                = min(amt, player.chips)
                player.chips      -= pay
                player.bet        += pay
                player.total_wager += pay
                self.pot          += pay
                if player.chips == 0: player.all_in = True

            self.current_bet = bb_p.bet

            start_node              = self.players.index(bb_p)
            self.current_player_idx = (start_node + 1) % len(self.players)
            for _ in range(len(self.players)):
                p = self.players[self.current_player_idx]
                if not p.is_bust and not p.folded and not p.all_in: break
                self.current_player_idx = (self.current_player_idx + 1) % len(self.players)

            embed = discord.Embed(
                title=f"🔥 홀덤 핸드 시작 (SB: {self.sb:,} / BB: {self.bb:,})",
                color=0x00ff00
            )
            embed.add_field(name="딜러", value=survivors[dealer_ptr].member.display_name)
            embed.add_field(name="SB",   value=sb_p.member.display_name)
            embed.add_field(name="BB",   value=bb_p.member.display_name)

        else:
            ante = self.sb
            for p in self.players:
                p.reset_hand()
                if not p.is_bust:
                    pay            = min(ante, p.chips)
                    p.chips       -= pay
                    p.bet         += pay
                    p.total_wager += pay
                    self.pot      += pay
                    if p.chips == 0: p.all_in = True
                    p.cards_hidden = [deal_card(self.deck), deal_card(self.deck)]
                    p.cards_open   = [deal_card(self.deck)]
                    p.hole_cards   = p.cards_hidden + p.cards_open

            self.current_bet = ante
            self.set_first_actor_seven()
            embed = discord.Embed(
                title=f"🃏 세븐 포커 핸드 시작 (앤티: {ante:,})",
                description="2장 히든, 1장 공개로 시작합니다.",
                color=0x00ff00
            )

        await self.broadcast(embed=embed, view=ShowHandView(self))
        await self.announce_turn()

    async def next_turn_or_street(self):
        if self.is_destroyed: return
        active = self.active_players()
        if len(active) == 1:
            return await self.end_hand_premature()

        players_in   = [p for p in active if not p.all_in]
        all_acted    = all(p.acted for p in players_in)
        bets_matched = all(p.bet == self.current_bet for p in players_in)

        if not players_in or (all_acted and bets_matched):
            await self.return_uncalled_bets()
            await self.advance_street()
            return

        for _ in range(len(self.players)):
            self.current_player_idx = (self.current_player_idx + 1) % len(self.players)
            p = self.players[self.current_player_idx]
            if not p.folded and not p.is_bust and not p.all_in:
                break
        await self.announce_turn()

    async def advance_street(self):
        self.betting_round += 1
        for p in self.players: p.reset_round()
        self.current_bet = 0
        self.min_raise   = self.bb

        if self.game_type == "holdem":
            if   self.betting_round == 1: self.community = [deal_card(self.deck) for _ in range(3)]; stage = "FLOP"
            elif self.betting_round == 2: self.community.append(deal_card(self.deck));                stage = "TURN"
            elif self.betting_round == 3: self.community.append(deal_card(self.deck));                stage = "RIVER"
            else: return await self.do_showdown()

            board_str = "  ".join(card_str(c) for c in self.community)
            hint_str  = board_best_hand_hint(self.community)
            embed = discord.Embed(title=f"🎴 **{stage}**", description=f"# {board_str}\n{hint_str}", color=0x5865F2)

        else:
            if   self.betting_round == 1: [p.cards_open.append(deal_card(self.deck)) or setattr(p, 'hole_cards', p.cards_hidden + p.cards_open) for p in self.active_players()]; stage = "4번째 카드 (공개)"
            elif self.betting_round == 2: [p.cards_open.append(deal_card(self.deck)) or setattr(p, 'hole_cards', p.cards_hidden + p.cards_open) for p in self.active_players()]; stage = "5번째 카드 (공개)"
            elif self.betting_round == 3: [p.cards_open.append(deal_card(self.deck)) or setattr(p, 'hole_cards', p.cards_hidden + p.cards_open) for p in self.active_players()]; stage = "6번째 카드 (공개)"
            elif self.betting_round == 4: [p.cards_hidden.append(deal_card(self.deck)) or setattr(p, 'hole_cards', p.cards_hidden + p.cards_open) for p in self.active_players()]; stage = "7번째 카드 (히든)"
            else: return await self.do_showdown()

            embed = discord.Embed(title=f"🎴 **{stage}**", description="카드가 배분되었습니다.", color=0x5865F2)

        embed.add_field(name="Pot",           value=f"{self.pot:,} 칩")
        embed.add_field(name="👥 Table Status", value=self.get_table_status_str(), inline=False)
        await self.broadcast(embed=embed, view=ShowHandView(self))

        if not self.players_can_act():
            await asyncio.sleep(2)
            await self.advance_street()
            return

        if self.game_type == "holdem":
            survivors   = self.survivor_players()
            count       = len(survivors)
            dealer_ptr  = self.dealer_idx % count
            sb_survivor = survivors[(dealer_ptr + 1) % count]
            sb_idx = self.players.index(sb_survivor)
            self.current_player_idx = sb_idx
            for _ in range(len(self.players)):
                p = self.players[self.current_player_idx]
                if not p.folded and not p.is_bust and not p.all_in: break
                self.current_player_idx = (self.current_player_idx + 1) % len(self.players)
        else:
            self.set_first_actor_seven()

        await self.announce_turn()

    async def announce_turn(self):
        if self.is_destroyed: return
        if self.action_msg:
            try: await self.action_msg.delete()
            except: pass

        p = self.players[self.current_player_idx]

        if p.pre_action:
            action    = p.pre_action; p.pre_action = None
            call_cost = self.current_bet - p.bet
            self.turn_start_time = time.time()
            if action == "fold":
                act_str = "fold" if call_cost > 0 else "check"
            elif action == "call":
                act_str = "call" if call_cost > 0 else "check"
            else:
                act_str = action
            await self.process_action(None, act_str)
            return

        to_call = self.current_bet - p.bet
        expiry  = int(time.time()) + self.timeout_seconds
        pos     = (self.get_position_name(self.current_player_idx)
                   if self.game_type == "holdem"
                   else ("선공" if to_call == 0 else "후공"))

        embed = discord.Embed(
            description=f"## ▶️ [{pos}] {p.member.mention} 차례\n⏳ **남은 시간:** <t:{expiry}:R>",
            color=0xFEE75C
        )
        embed.add_field(name="Pot",   value=f"**{self.pot:,}**", inline=True)
        embed.add_field(name="Call",  value=f"**{to_call:,}**",  inline=True)
        embed.add_field(name="Stack", value=f"**{p.chips:,}**",  inline=True)
        if self.game_type == "holdem" and self.community:
            board_str = "  ".join(card_str(c) for c in self.community)
            embed.add_field(name="Board", value=f"### {board_str}", inline=False)
        embed.add_field(name="👥 Table Status", value=self.get_table_status_str(), inline=False)

        self.action_msg = await self.broadcast(content=p.member.mention, embed=embed, view=ActionView(self))
        await self.start_turn_timer()

    async def process_action(self, interaction, act_type, amount=0):
        self.cancel_turn_timer()
        p       = self.players[self.current_player_idx]
        is_snap = (time.time() - self.turn_start_time) < 1.0
        prefix  = "⚡ **SNAP** " if is_snap else ""

        async def reply_err(content):
            if interaction:
                if not interaction.response.is_done():
                    await interaction.response.send_message(content, ephemeral=True)
                else:
                    await interaction.followup.send(content, ephemeral=True)
            else:
                await self.channel.send(content)

        async def reply_ok(content):
            if interaction:
                if not interaction.response.is_done():
                    await interaction.response.send_message(content, ephemeral=True)
                else:
                    await interaction.followup.send(content, ephemeral=True)
                await send_private_hand_info(interaction, self)
            else:
                await self.channel.send(content)

        is_bet_increase = False
        msg = ""

        if act_type == "fold":
            p.folded = True
            msg = f"{prefix}FOLD"

        elif act_type == "check":
            if p.bet < self.current_bet:
                await reply_err("❌ 현재 베팅이 있어 체크가 불가합니다.")
                await self.start_turn_timer()
                return
            msg = f"{prefix}CHECK"

        elif act_type == "call":
            pay            = min(self.current_bet - p.bet, p.chips)
            p.chips       -= pay; p.bet += pay
            p.total_wager += pay; self.pot += pay
            if p.chips == 0: p.all_in = True; msg = f"🔥 {prefix}ALL-IN CALL"
            else:                              msg = f"📞 {prefix}CALL"

        elif act_type == "raise":
            total  = self.current_bet + amount
            needed = total - p.bet
            if needed > p.chips:
                await reply_err(f"❌ 칩이 부족합니다. (필요: {needed:,} / 보유: {p.chips:,})")
                await self.start_turn_timer(); return
            min_raise_amount = self.bb if self.game_type == "seven" else self.min_raise
            max_raise_amount = max(self.bb, self.current_bet) if self.game_type == "seven" else p.chips
            if amount < min_raise_amount:
                await reply_err(f"❌ 최소 레이즈 금액: {min_raise_amount:,}")
                await self.start_turn_timer(); return
            if amount > max_raise_amount:
                await reply_err(f"❌ 최대 레이즈 금액: {max_raise_amount:,}")
                await self.start_turn_timer(); return
            p.chips       -= needed; p.bet = total
            p.total_wager += needed; self.pot += needed
            self.current_bet = total; self.min_raise = amount
            for o in self.active_players():
                if o != p: o.acted = False
            msg = f"⬆️ RAISE (+{amount:,})"
            is_bet_increase = True

        elif act_type == "allin":
            pay            = p.chips
            p.chips        = 0; p.bet += pay
            p.total_wager += pay; self.pot += pay; p.all_in = True
            if p.bet > self.current_bet:
                diff = p.bet - self.current_bet
                if diff >= self.min_raise: self.min_raise = diff
                for o in self.active_players():
                    if o != p: o.acted = False
                self.current_bet = p.bet
                is_bet_increase  = True
            msg = f"🔥🔥 {prefix}ALL-IN ({pay:,})"

        p.acted = True
        if is_bet_increase:
            cleared = sum(1 for player in self.players if player.pre_action)
            for player in self.players: player.pre_action = None
            if cleared > 0: msg += " (⚠️ 예약 초기화)"

        action_log = f"[{self.get_position_name(self.current_player_idx)}] {p.member.display_name}: {msg}"
        if interaction:
            await self.broadcast(content=action_log)
        await self.log(action_log)
        await reply_ok(f"✅ **{act_type.upper()}** 처리 완료.")
        await self.next_turn_or_street()

    async def do_showdown(self):
        active     = self.active_players()
        board_desc = (f"# {' '.join(card_str(c) for c in self.community)}"
                      if self.game_type == "holdem"
                      else "전 플레이어 핸드를 공개합니다!")
        embed = discord.Embed(title="🎊 SHOWDOWN", description=board_desc, color=0xFFD700)

        ranked = []
        for p in active:
            score, kickers, best5 = eval_hand(p.hole_cards, self.use_back_straight)
            ranked.append((p, (score, kickers), best5))

            rank_name_str = hand_name(score)
            if self.use_back_straight and score in (4, 8) and kickers[0] == 13:
                rank_name_str = "✨ 백 스트레이트 플러시" if score == 8 else "✨ 백 스트레이트"
            effect = "\n✨🌈 **SPECIAL HAND!** 🌈✨" if score >= 6 else ""

            if self.game_type == "seven":
                all_cards = " ".join(card_str(c) for c in p.hole_cards)
                embed.add_field(name=p.member.display_name, value=f"{all_cards}\n**{rank_name_str}**{effect}", inline=False)
            else:
                embed.add_field(
                    name=p.member.display_name,
                    value=f"{card_str(p.hole_cards[0])} {card_str(p.hole_cards[1])}\n**{rank_name_str}**{effect}",
                    inline=True
                )

        await self.broadcast(embed=embed)
        ranked.sort(key=lambda x: x[1], reverse=True)

        contracts = {p: p.total_wager for p in self.players}
        log       = []

        while any(v > 0 for v in contracts.values()):
            if not ranked: break
            best_score = ranked[0][1]
            tier       = [item for item in ranked if item[1] == best_score]
            winners    = [item[0] for item in tier]
            valid_w    = [w for w in winners if contracts.get(w, 0) > 0]

            if not valid_w:
                ranked = [item for item in ranked if item[1] != best_score]
                continue
            valid_w = self.dealer_order(valid_w)

            min_w = min(contracts[w] for w in valid_w)
            chunk = 0
            for p in self.players:
                take         = min(contracts.get(p, 0), min_w)
                contracts[p] = contracts.get(p, 0) - take
                chunk       += take

            share     = chunk // len(valid_w)
            remainder = chunk % len(valid_w)
            for i, w in enumerate(valid_w):
                gain = share + (1 if i < remainder else 0)
                w.chips += gain
                log.append(f"🏆 {w.member.display_name} +{gain:,}")

            ranked = [item for item in ranked if contracts.get(item[0], 0) > 0 or item[1] != best_score]

        leftover = sum(v for v in contracts.values() if v > 0)
        if leftover > 0 and self.survivor_players():
            fallback = self.dealer_order(self.survivor_players())[0]
            fallback.chips += leftover
            log.append(f"🏆 {fallback.member.display_name} +{leftover:,} (잔여 팟)")

        result_str = "💰 **핸드 결과**\n" + "\n".join(log)
        await self.broadcast(content=result_str)
        await self.log(result_str)
        await self.finalize_hand()

    async def end_hand_premature(self):
        w     = self.active_players()[0]
        total = sum(p.total_wager for p in self.players)
        w.chips += total
        msg = f"🏆 **{w.member.display_name}** 승리! (상대 폴드) +{total:,}"
        await self.broadcast(content=msg)
        await self.log(msg)
        await self.finalize_hand()

    async def finalize_hand(self):
        if self.is_destroyed: return
        self.hand_active = False
        self.cancel_turn_timer()

        old_survivors = self.survivor_players()
        old_dealer    = old_survivors[self.dealer_idx % len(old_survivors)] if old_survivors else None
        busts = []
        for p in self.players:
            if p.chips <= 0 and not p.is_bust:
                p.is_bust = True
                busts.append(p)
                if p not in self.eliminated_players:
                    self.eliminated_players.append(p)

        if busts:
            self.rebuy_period = True
            rebuy_msg = await self.broadcast(
                content=f"💀 **전력 소실:** {', '.join(b.member.display_name for b in busts)}\n"
                        f"⏳ **5초간 리바이인 가능!**",
                view=RebuyView(self)
            )
            await asyncio.sleep(5)
            self.rebuy_period = False

            if rebuy_msg:
                try:
                    dv = RebuyView(self)
                    for c in dv.children: c.disabled = True
                    await rebuy_msg.edit(content="🔒 **리바이인 마감**", view=dv)
                except: pass

            kicked = []
            for p in busts:
                if p.chips <= 0:
                    if p in self.players: self.players.remove(p)
                    GLOBAL_PLAYING_USERS.discard(p.member.id)
                    kicked.append(p.member.display_name)
            if kicked:
                await self.broadcast(content=f"👋 **{', '.join(kicked)}** 님이 리바이인 없이 퇴장했습니다.")
        else:
            await asyncio.sleep(3)

        survivors_after = self.survivor_players()
        if survivors_after:
            if old_dealer and old_dealer in survivors_after:
                self.dealer_idx = (survivors_after.index(old_dealer) + 1) % len(survivors_after)
            elif old_dealer and old_dealer in old_survivors:
                old_pos = old_survivors.index(old_dealer)
                for step in range(1, len(old_survivors) + 1):
                    candidate = old_survivors[(old_pos + step) % len(old_survivors)]
                    if candidate in survivors_after:
                        self.dealer_idx = survivors_after.index(candidate)
                        break
                else:
                    self.dealer_idx = 0
            else:
                self.dealer_idx = 0

        self._invalidate_aborted_records()

        survivors = self.survivor_players()
        if   len(survivors) >= 2: await self.start_hand()
        elif len(survivors) == 1: await self.end_tournament(survivors[0])
        else:                     await self.end_tournament(None)

    async def end_tournament(self, winner):
        try:
            if self.blind_timer_task:
                self.blind_timer_task.cancel()
                self.blind_timer_task = None
            self.game_started = False

            rankings = [winner] if winner else []
            rankings.extend(reversed(self.eliminated_players))
            seen = set(); unique_rankings = []
            for r in rankings:
                if r and r.member.id not in seen:
                    seen.add(r.member.id); unique_rankings.append(r)
            rankings = unique_rankings

            record_game_result(
                rankings, winner, self.entry_cost,
                self.table_id, self.game_type, self.prize_pool
            )

            total_p = len(rankings)
            if   total_p == 1: payouts = [(0, 1.0)]
            elif total_p == 2: payouts = [(0, 0.8), (1, 0.2)]
            elif total_p == 3: payouts = [(0, 0.6), (1, 0.3), (2, 0.1)]
            elif total_p == 4: payouts = [(0, 0.7), (1, 0.3)]
            else:              payouts = [(0, 0.5), (1, 0.3), (2, 0.2)]

            medals = ["🥇", "🥈", "🥉"]
            log    = [f"🏆 **작전 종료!** (총 상금: {self.prize_pool:,} 청휘석)"]

            for r_idx, ratio in payouts:
                if r_idx < len(rankings):
                    p     = rankings[r_idx]
                    prize = int(self.prize_pool * ratio)
                    if self.entry_cost > 0:
                        update_balance(p.member.id, prize)
                    new_bal = get_user_balance(p.member.id)
                    icon = medals[r_idx] if r_idx < 3 else "🏅"
                    log.append(f"{icon} {r_idx+1}등: **{p.member.display_name}** (+{prize:,} 청휘석) | 잔액: {new_bal:,}")

            result_str = "\n".join(log)
            await self.broadcast(content=result_str)
            await self.log(result_str)
            await self.channel.send("📋 **게임이 종료되어 방이 초기화되었습니다.**")

        finally:
            for p in list(self.players):
                GLOBAL_PLAYING_USERS.discard(p.member.id)
            self._invalidate_aborted_records()
            if self.table_id in bot.tables:
                del bot.tables[self.table_id]

# ==========================================
# 🤖 Bot
# ==========================================
def make_state_backup_payload():
    tables = []
    for table_id, table in bot.tables.items():
        tables.append({
            "table_id":    table_id,
            "channel_id":  table.channel.id if table.channel else None,
            "host_id":     table.host.id if table.host else None,
            "game_type":   table.game_type,
            "entry_cost":  table.entry_cost,
            "game_started": table.game_started,
            "hand_active": table.hand_active,
            "sb":          table.sb,
            "bb":          table.bb,
            "pot":         table.pot,
            "dealer_idx":  table.dealer_idx,
            "players": [
                {
                    "user_id": p.member.id,
                    "chips":   p.chips,
                    "is_bust": p.is_bust,
                    "folded":  p.folded,
                    "all_in":  p.all_in,
                }
                for p in table.players
            ],
        })
    return {
        "created_at":      datetime.datetime.now().isoformat(),
        "tables":          tables,
        "blackjack_users": list(BLACKJACK_GAMES.keys()),
    }

async def backup_state_loop():
    while True:
        try:
            payload = make_state_backup_payload()
            with open(STATE_BACKUP_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[State Backup Error] {e}")
        await asyncio.sleep(60)

class PokerBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS)
        self.tables      = {}
        self.backup_task = None

    async def setup_hook(self):
        await self.tree.sync()

    async def on_application_command_error(self, interaction: discord.Interaction, error):
        msg = f"⚠️ 오류가 발생했습니다: `{error}`"
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except: pass
        print(f"[Command Error] {error}")

bot = PokerBot()

@bot.event
async def on_ready():
    init_db()
    if not bot.backup_task or bot.backup_task.done():
        bot.backup_task = asyncio.create_task(backup_state_loop())
    print(f"✅ Logged in: {bot.user} | 테이블: {len(bot.tables)}")

@bot.event
async def on_error(event, *args, **kwargs):
    import traceback
    print(f"[on_error] {event}\n{traceback.format_exc()}")

def get_channel_table(channel_id):
    return next((table for table in bot.tables.values() if table.channel.id == channel_id), None)

# ==========================================
# 📋 슬래시 커맨드
# ==========================================
@bot.tree.command(name="전적", description="내 전적 및 승률을 확인합니다.")
async def stats(interaction: discord.Interaction):
    row   = get_user_stats_all(interaction.user.id)
    wins  = row['wins_total']; games = row['games_total']
    rate  = (wins / games * 100) if games > 0 else 0.0

    embed = discord.Embed(title=f"📊 {interaction.user.display_name} 선생님의 전투 기록", color=0x3498db)
    embed.add_field(name="🏆 총 승리", value=f"{wins}회",        inline=True)
    embed.add_field(name="🎮 총 게임", value=f"{games}판",        inline=True)
    embed.add_field(name="📈 승률",    value=f"**{rate:.1f}%**", inline=True)

    table_str = "```\n      |  2인  |  3인  |  4인  |  5인  |  6인\n------+-------+-------+-------+-------+------\n"
    for cost in [0, 1, 3, 10]:
        line = f"{cost:<2}k  |"
        for p in range(2, 7):
            w = row[f"w_{cost}k_{p}p"]
            line += f" {w:^5} |"
        table_str += line[:-1] + "\n"
    table_str += "```"
    embed.add_field(name="상세 전적 (승수)", value=table_str, inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="순위", description="서버 내 다양한 순위를 조회합니다.")
@app_commands.choices(category=[
    app_commands.Choice(name="💰 부자 랭킹 (보유 청휘석)",  value="balance"),
    app_commands.Choice(name="🏆 전체 통합 승수",           value="wins_total"),
    app_commands.Choice(name="🎮 전체 최다 플레이",         value="games_total"),
    app_commands.Choice(name="💵 0 청휘석(무료) 최다승",    value="agg_cost_0k"),
    app_commands.Choice(name="💵 1,000 청휘석 최다승",      value="agg_cost_1k"),
    app_commands.Choice(name="💵 3,000 청휘석 최다승",      value="agg_cost_3k"),
    app_commands.Choice(name="💵 10,000 청휘석 최다승",     value="agg_cost_10k"),
])
async def rank(interaction: discord.Interaction, category: str):
    con   = db(); cur = con.cursor()
    table = "stats"; target_column = category; order_by = category; unit = "승"

    if category == "balance":
        table = "users"; unit = "청휘석"
    elif category == "games_total":
        unit = "판"
    elif category.startswith("agg_cost_"):
        cost          = category.split("_")[-1].replace("k", "")
        sum_query     = " + ".join(f"w_{cost}k_{p}p" for p in range(2, 7))
        target_column = f"({sum_query}) as total_wins"
        order_by      = "total_wins"

    try:
        cur.execute(f"SELECT user_id, {target_column} FROM {table} ORDER BY {order_by} DESC LIMIT 10")
        rows = cur.fetchall()
    except:
        rows = []
    con.close()

    embed  = discord.Embed(title=f"🏆 랭킹 — {category}", color=0xFFD700)
    medals = ["🥇", "🥈", "🥉"]
    for idx, row in enumerate(rows, 1):
        uid  = row[0]; val = row[1]
        user = interaction.guild.get_member(uid)
        name = user.display_name if user else "Unknown"
        icon = medals[idx-1] if idx <= 3 else f"{idx}위"
        embed.add_field(name=f"{icon} {name}", value=f"**{val:,}** {unit}", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="내게임", description="현재 참가 중인 게임 정보를 확인합니다.")
async def my_game(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid not in GLOBAL_PLAYING_USERS:
        return await interaction.response.send_message("현재 참가 중인 게임이 없습니다.", ephemeral=True)

    bj = BLACKJACK_GAMES.get(uid)
    if bj:
        stage = "딜러 선택 중" if bj.get("stage") == "select" else "진행 중"
        embed = discord.Embed(title="🎮 내 게임 정보", color=0x3498db)
        embed.add_field(name="종목",   value="블랙잭",                       inline=True)
        embed.add_field(name="상태",   value=stage,                           inline=True)
        embed.add_field(name="베팅액", value=f"{bj.get('bet', 0):,} 청휘석", inline=True)
        embed.add_field(name="채널",   value=interaction.channel.mention,     inline=True)
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    for table in bot.tables.values():
        p = table.get_player(uid)
        if p:
            g_name = "텍사스 홀덤" if table.game_type == "holdem" else "세븐 포커"
            status = "진행 중" if table.game_started else "대기 중"
            embed  = discord.Embed(title="🎮 내 게임 정보", color=0x3498db)
            embed.add_field(name="종목",      value=g_name,                         inline=True)
            embed.add_field(name="상태",      value=status,                          inline=True)
            embed.add_field(name="참가비",    value=f"{table.entry_cost:,} 청휘석",  inline=True)
            embed.add_field(name="참가 인원", value=f"{len(table.players)}명",       inline=True)
            embed.add_field(name="내 칩",     value=f"{p.chips:,}",                  inline=True)
            embed.add_field(name="채널",      value=table.channel.mention,           inline=True)
            return await interaction.response.send_message(embed=embed, ephemeral=True)

    await interaction.response.send_message("게임 정보를 찾을 수 없습니다.", ephemeral=True)


@bot.tree.command(name="포기", description="진행 중인 포커 게임에서 기권합니다. (보유 칩은 몰수됩니다)")
async def forfeit(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid not in GLOBAL_PLAYING_USERS:
        return await interaction.response.send_message("❌ 현재 참가 중인 게임이 없습니다.", ephemeral=True)

    bj = BLACKJACK_GAMES.get(uid)
    if bj:
        view  = bj.get("view")
        bet   = bj.get("bet", 0)
        stage = bj.get("stage")
        BLACKJACK_GAMES.pop(uid, None)
        GLOBAL_PLAYING_USERS.discard(uid)
        if stage == "select":
            update_balance(uid, bet)
            if view:
                view._ended = True; view.clear_items()
                if view.message:
                    try: await view.message.edit(view=None)
                    except: pass
            return await interaction.response.send_message(
                "🏳️ 블랙잭을 포기했습니다. 카드 공개 전이라 베팅액을 반환했습니다.", ephemeral=True
            )
        if view:
            view._ended = True; view.clear_items()
            if view.message:
                try:
                    embed = view.make_embed(end_game=True, result_msg=view.dialogue["lose"], color=0xFF0000)
                    await view.message.edit(embed=embed, view=None)
                except: pass
        return await interaction.response.send_message(
            "🏳️ 블랙잭을 포기했습니다. 이번 판은 패배 처리됩니다.", ephemeral=True
        )

    target_table = None
    for table in bot.tables.values():
        if table.get_player(uid) and table.game_started:
            target_table = table; break

    if not target_table:
        return await interaction.response.send_message("❌ 진행 중인 게임을 찾을 수 없습니다.", ephemeral=True)

    p = target_table.get_player(uid)
    if p.folded or p.is_bust:
        return await interaction.response.send_message("❌ 이미 탈락 상태입니다.", ephemeral=True)

    p.folded  = True
    p.is_bust = True
    p.chips   = 0
    if p not in target_table.eliminated_players:
        target_table.eliminated_players.append(p)

    GLOBAL_PLAYING_USERS.discard(uid)
    if p in target_table.players:
        target_table.players.remove(p)

    await interaction.response.send_message(
        f"🏳️ **{interaction.user.display_name}** 선생님이 작전에서 기권했습니다. (보유 칩 몰수)", ephemeral=True
    )
    await target_table.broadcast(content=f"🏳️ **{interaction.user.display_name}** 선생님이 기권하여 퇴장했습니다.")

    survivors = target_table.survivor_players()
    if len(survivors) < 2:
        if target_table.hand_active:
            target_table.cancel_turn_timer()
            await target_table.finalize_hand()
    elif target_table.hand_active:
        cur_p = target_table.players[target_table.current_player_idx] if target_table.players else None
        if cur_p == p:
            await target_table.next_turn_or_street()


@bot.tree.command(name="admin_강제종료", description="[관리자] 진행 중인 게임을 강제 종료합니다.")
@app_commands.describe(channel="게임이 진행 중인 채널")
async def admin_force_end(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_authorized_admin(interaction):
        return await interaction.response.send_message("⚠️ 권한이 없습니다.", ephemeral=True)

    target = None
    for table in bot.tables.values():
        if table.channel.id == channel.id:
            target = table; break

    if not target:
        ended_blackjack = []
        for uid, bj in list(BLACKJACK_GAMES.items()):
            if bj.get("channel_id") == channel.id:
                view = bj.get("view")
                bet  = bj.get("bet", 0)
                update_balance(uid, bet)
                GLOBAL_PLAYING_USERS.discard(uid)
                BLACKJACK_GAMES.pop(uid, None)
                if view:
                    view._ended = True; view.clear_items()
                    if view.message:
                        try:
                            embed = discord.Embed(
                                title="🛑 게임 강제 종료",
                                description="관리자 권한으로 블랙잭이 종료되었습니다. 베팅액이 반환되었습니다.",
                                color=0x95a5a6
                            )
                            await view.message.edit(embed=embed, view=None)
                        except: pass
                ended_blackjack.append(uid)
        if ended_blackjack:
            await interaction.response.send_message(
                f"✅ {channel.mention}의 블랙잭 {len(ended_blackjack)}건을 강제 종료하고 베팅액을 반환했습니다.",
                ephemeral=True
            )
            return await channel.send("🛑 **관리자 권한으로 블랙잭 게임이 강제 종료되었습니다. 베팅액이 반환되었습니다.**")
        return await interaction.response.send_message("❌ 해당 채널에서 진행 중인 게임이 없습니다.", ephemeral=True)

    result = await target.force_finish_table()
    if result == "finished":
        await interaction.response.send_message(
            f"✅ {channel.mention}의 게임을 현재 상태 기준으로 종료 정산했습니다.", ephemeral=True
        )
        await channel.send("🛑 **관리자 권한으로 게임이 현재 상태 기준 종료되었습니다.**")
    else:
        await interaction.response.send_message(
            f"✅ {channel.mention}의 대기 중 테이블을 취소하고 참가비를 반환했습니다.", ephemeral=True
        )
        await channel.send("🛑 **관리자 권한으로 대기 중 테이블이 취소되었습니다. 참가비가 반환되었습니다.**")


@bot.tree.command(name="admin_reset_stats", description="[관리자] 전적 초기화 (유저 미지정 시 전체)")
async def reset_stats(interaction: discord.Interaction, user: discord.Member = None):
    if not is_authorized_admin(interaction):
        return await interaction.response.send_message("⚠️ 권한이 없습니다.", ephemeral=True)
    reset_stats_db(user.id if user else None)
    tgt = user.display_name if user else "전체 유저"
    await interaction.response.send_message(f"✅ **{tgt}**의 전적이 초기화되었습니다.", ephemeral=True)


@bot.tree.command(name="admin_money", description="[관리자] 특정 유저 또는 전체 유저의 자산을 설정합니다.")
@app_commands.describe(amount="설정할 액수", user="대상 유저 (미지정 시 전체 유저에게 적용)")
async def admin_money(interaction: discord.Interaction, amount: int, user: discord.Member = None):
    if not is_authorized_admin(interaction):
        return await interaction.response.send_message("⚠️ 권한이 없습니다.", ephemeral=True)
    if user:
        set_balance(user.id, amount)
        await interaction.response.send_message(
            f"⚙️ **{user.display_name}** 선생님의 자산을 **{amount:,} 청휘석**으로 조정했습니다.", ephemeral=True
        )
    else:
        set_all_balances(amount)
        embed = discord.Embed(
            title="⚙️ 전체 자산 일괄 설정",
            description=f"모든 유저의 자산을 **{amount:,} 청휘석**으로 설정했습니다.",
            color=0x808080
        )
        embed.set_footer(text=f"집행: {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="admin_전체지급", description="[관리자] 모든 유저에게 지원금을 지급합니다.")
@app_commands.describe(amount="지급할 금액 (음수 입력 시 차감)")
async def admin_give_all(interaction: discord.Interaction, amount: int):
    if not is_authorized_admin(interaction):
        return await interaction.response.send_message("⚠️ 권한이 없습니다.", ephemeral=True)
    if amount == 0:
        return await interaction.response.send_message("❌ 0 청휘석은 지급할 수 없습니다.", ephemeral=True)
    update_all_balances(amount)
    if amount > 0:
        embed = discord.Embed(
            title="🎁 전 서버 특별 지원금!",
            description=f"전원에게 **{amount:,} 청휘석** 지급!",
            color=0x2ecc71
        )
    else:
        embed = discord.Embed(
            title="🚨 자산 긴급 회수",
            description=f"전원에게서 **{abs(amount):,} 청휘석** 회수.",
            color=0xe74c3c
        )
    embed.set_footer(text=f"집행: {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="출석", description="매일 2,000 청휘석을 수령합니다.")
async def daily(interaction: discord.Interaction):
    if daily_check(interaction.user.id):
        bal = get_user_balance(interaction.user.id)
        await interaction.response.send_message(f"✅ 출석 보상 +2,000 청휘석! (잔액: {bal:,})", ephemeral=True)
    else:
        await interaction.response.send_message("📅 오늘 이미 출석했습니다. 내일 다시 오세요!", ephemeral=True)


@bot.tree.command(name="정보", description="내 자산을 확인합니다.")
async def wallet(interaction: discord.Interaction):
    bal = get_user_balance(interaction.user.id)
    await interaction.response.send_message(f"💼 보유 청휘석: **{bal:,}**", ephemeral=True)


@bot.tree.command(name="블랙잭", description="아리스 혹은 케이와 블랙잭 승부!")
@app_commands.describe(amount="베팅할 금액")
async def blackjack(interaction: discord.Interaction, amount: int):
    if interaction.user.id in GLOBAL_PLAYING_USERS:
        return await interaction.response.send_message("❌ 이미 다른 게임에 참가 중입니다.", ephemeral=True)
    if amount <= 0:
        return await interaction.response.send_message("❌ 0보다 큰 금액을 입력하세요.", ephemeral=True)
    bal = get_user_balance(interaction.user.id)
    if bal < amount:
        return await interaction.response.send_message(f"❌ 잔액 부족 (보유: {bal:,} 청휘석)", ephemeral=True)

    update_balance(interaction.user.id, -amount)
    GLOBAL_PLAYING_USERS.add(interaction.user.id)
    embed = discord.Embed(
        title="🎮 블랙잭 시스템 부팅",
        description="**딜러를 선택해 주세요.**\n\n🧹 **아리스**: 명랑쾌활 딜러!\n⚙️ **케이**: 냉철한 연산 딜러!",
        color=0xced6e0
    )
    embed.set_footer(text=f"베팅액: {amount:,} 청휘석 | 잘못 눌렀다면 [게임 종료]")
    view = BlackjackCharacterSelectView(interaction.user, amount)
    BLACKJACK_GAMES[interaction.user.id] = {
        "stage":      "select",
        "view":       view,
        "bet":        amount,
        "channel_id": interaction.channel.id if interaction.channel else None,
    }
    try:
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()
    except Exception:
        BLACKJACK_GAMES.pop(interaction.user.id, None)
        GLOBAL_PLAYING_USERS.discard(interaction.user.id)
        update_balance(interaction.user.id, amount)
        raise


@bot.tree.command(name="생성", description="포커 대기방을 생성합니다.")
@app_commands.choices(entry=[app_commands.Choice(name=f"{x:,} 청휘석", value=x) for x in [0, 1000, 3000, 10000]])
@app_commands.choices(game_type=[
    app_commands.Choice(name="🃏 텍사스 홀덤",      value="holdem"),
    app_commands.Choice(name="🃏 세븐 포커 (정통)", value="seven"),
])
@app_commands.choices(back_straight=[
    app_commands.Choice(name="🔺 백 스트레이트 적용 (마운틴 다음)", value="true"),
    app_commands.Choice(name="🔻 일반 룰 (최하위 스트레이트)",       value="false"),
])
@app_commands.describe(
    timeout="턴 제한 시간 (초, 기본 30)",
    blind_interval_min="블라인드 업 주기 (분, 기본 15)",
    blind_multiplier="블라인드 증가 배수 (기본 2)"
)
async def create(
    interaction: discord.Interaction,
    entry: int,
    game_type: str = "holdem",
    back_straight: str = "false",
    timeout: app_commands.Range[int, 10, 300] = 30,
    blind_interval_min: app_commands.Range[int, 1, 60] = 15,
    blind_multiplier: app_commands.Range[int, 2, 4] = 2,
):
    if interaction.user.id in GLOBAL_PLAYING_USERS:
        return await interaction.response.send_message("❌ 이미 다른 게임에 참가 중입니다.", ephemeral=True)
    bal = get_user_balance(interaction.user.id)
    if entry > 0 and bal < entry:
        return await interaction.response.send_message(f"❌ 참가비 부족 (보유: {bal:,} 청휘석)", ephemeral=True)

    use_bs    = (back_straight == "true")
    table_id  = str(uuid.uuid4())
    new_table = Table(
        channel=interaction.channel,
        entry_cost=entry,
        host=interaction.user,
        table_id=table_id,
        game_type=game_type,
        use_back_straight=use_bs,
        timeout_seconds=timeout,
        blind_interval=blind_interval_min * 60,
        blind_multiplier=blind_multiplier,
    )
    bot.tables[table_id] = new_table

    if entry > 0:
        update_balance(interaction.user.id, -entry)
    GLOBAL_PLAYING_USERS.add(interaction.user.id)
    new_table.players.append(Player(interaction.user, new_table.starting_stack()))

    lobby_view = PokerLobbyView(new_table)
    await interaction.response.send_message(embed=lobby_view._make_lobby_embed(), view=lobby_view)

bot.run(BOT_TOKEN)
