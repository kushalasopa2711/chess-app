/* ================================================================
   ChessWager  –  Frontend App
   Chess board + WebSocket + Webcam anti-cheat + Wallet + Auth
   ================================================================ */

'use strict';

// ── Config ──────────────────────────────────────────────────────────────────
const BASE = '';  // same origin
const WS_PROTO = location.protocol === 'https:' ? 'wss' : 'ws';
const WS_HOST  = location.host;

// ── State ───────────────────────────────────────────────────────────────────
const S = {
  token:      null,
  user:       null,
  wallet:     null,
  game:       null,       // current game object
  myColor:    null,       // 'white' | 'black'
  ws:         null,       // WebSocket
  selected:   null,       // selected square name (e.g. 'e2')
  lastFrom:   null,
  lastTo:     null,
  boardFlip:  false,      // true when playing as black
  video:      null,       // VideoMonitor instance
  tabHiddenAt: null,
};

// ── Piece unicode map ────────────────────────────────────────────────────────
const PIECES = {
  K:'♔', Q:'♕', R:'♖', B:'♗', N:'♘', P:'♙',
  k:'♚', q:'♛', r:'♜', b:'♝', n:'♞', p:'♟',
};
const PIECE_NAMES = {
  K:'King', Q:'Queen', R:'Rook', B:'Bishop', N:'Knight', P:'Pawn',
};

// ════════════════════════════════════════════════════════════════════════════
//  API Layer
// ════════════════════════════════════════════════════════════════════════════
const API = {
  async req(method, path, body, isForm = false) {
    const headers = {};
    if (S.token) headers['Authorization'] = `Bearer ${S.token}`;
    if (body && !isForm) headers['Content-Type'] = 'application/json';
    const res = await fetch(BASE + path, {
      method,
      headers,
      body: isForm ? body : (body ? JSON.stringify(body) : undefined),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    return data;
  },
  get:    (p)       => API.req('GET',    p),
  post:   (p, b)    => API.req('POST',   p, b),
  delete: (p)       => API.req('DELETE', p),

  register:        (u,e,pw) => API.post('/auth/register',   {username:u,email:e,password:pw}),
  login:           (u,pw)   => API.post('/auth/login',      {username:u,password:pw}),
  me:              ()       => API.get('/auth/me'),
  getWallet:       ()       => API.get('/wallet/balance'),
  deposit:         (amt)    => API.post('/wallet/deposit',  {amount:amt}),
  withdraw:        (amt)    => API.post('/wallet/withdraw', {amount:amt}),
  transactions:    ()       => API.get('/wallet/transactions'),
  upiInfo:         (amt)    => API.get(`/deposit/upi-info?amount=${amt}`),
  submitDeposit:   (amt, utr) => {
    const fd = new FormData();
    fd.append('amount', amt);
    fd.append('utr_number', utr);
    return API.req('POST', '/deposit/request', fd, true);
  },
  myDepositReqs:   ()       => API.get('/deposit/my-requests'),
  myPayouts:       ()       => API.get('/deposit/my-payouts'),
  getGames:        (st)     => API.get(`/games${st?'?status='+st:''}`),
  createGame:      (bet)    => API.post('/games',           {bet_amount:bet}),
  getGame:         (id)     => API.get(`/games/${id}`),
  joinGame:        (id)     => API.post(`/games/${id}/join`),
  makeMove:        (id,mv)  => API.post(`/games/${id}/move`, {move:mv, client_timestamp:Date.now()}),
  resign:          (id)     => API.post(`/games/${id}/resign`),
  getUser:         (id)     => API.get(`/users/${id}`),
  myFlags:         ()       => API.get('/users/me/flags'),
  reportNoCamera:  (id, reason) => {
    const fd = new FormData(); fd.append('reason', reason);
    return API.req('POST', `/video/${id}/no-camera`, fd, true);
  },
  reportTabHidden: (id, ms) => {
    const fd = new FormData(); fd.append('duration_ms', ms);
    return API.req('POST', `/video/${id}/tab-hidden`, fd, true);
  },
  uploadChunk:     (id, blob, num) => {
    const fd = new FormData();
    fd.append('chunk', blob, `chunk_${num}.webm`);
    fd.append('chunk_number', num);
    return API.req('POST', `/video/${id}/chunk`, fd, true);
  },
};

// ════════════════════════════════════════════════════════════════════════════
//  Toast
// ════════════════════════════════════════════════════════════════════════════
const Toast = {
  show(msg, type='info', dur=3500) {
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    const icons = {success:'✅', error:'❌', warning:'⚠️', info:'ℹ️'};
    el.innerHTML = `<span>${icons[type]||'ℹ️'}</span><span>${msg}</span>`;
    document.getElementById('toasts').appendChild(el);
    setTimeout(() => el.remove(), dur);
  },
  ok:   (m) => Toast.show(m, 'success'),
  err:  (m) => Toast.show(m, 'error', 4500),
  warn: (m) => Toast.show(m, 'warning'),
};

// ════════════════════════════════════════════════════════════════════════════
//  Modal
// ════════════════════════════════════════════════════════════════════════════
const Modal = {
  _cb: null,
  show(icon, title, body, confirmLabel='OK', cancelLabel='Cancel') {
    return new Promise(resolve => {
      Modal._cb = resolve;
      document.getElementById('modal-icon').textContent  = icon;
      document.getElementById('modal-title').textContent = title;
      document.getElementById('modal-body').textContent  = body;
      document.getElementById('modal-ok').textContent    = confirmLabel;
      document.getElementById('modal-cancel').textContent= cancelLabel;
      document.getElementById('modal-overlay').classList.add('open');
    });
  },
  close(val) {
    document.getElementById('modal-overlay').classList.remove('open');
    if (Modal._cb) { Modal._cb(val); Modal._cb = null; }
  },
};

// ════════════════════════════════════════════════════════════════════════════
//  View routing
// ════════════════════════════════════════════════════════════════════════════
function showView(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  const v = document.getElementById(`view-${name}`);
  if (v) v.classList.add('active');

  // Update active nav link
  document.querySelectorAll('.nav-link').forEach(l => {
    l.classList.toggle('active', l.dataset.view === name);
  });
  window.scrollTo(0, 0);
}

// ════════════════════════════════════════════════════════════════════════════
//  FEN / Chess Board
// ════════════════════════════════════════════════════════════════════════════
function parseFEN(fen) {
  const [board, turn] = fen.split(' ');
  const grid = [];
  for (const rank of board.split('/')) {
    const row = [];
    for (const ch of rank) {
      if ('12345678'.includes(ch)) {
        for (let i = 0; i < +ch; i++) row.push(null);
      } else {
        row.push(ch);
      }
    }
    grid.push(row);
  }
  return { grid, turn };  // grid[0]=rank8, grid[7]=rank1
}

function sqName(row, col, flipped) {
  const files = 'abcdefgh';
  if (flipped) return files[7 - col] + (row + 1);
  return files[col] + (8 - row);
}

function squareToRowCol(sq, flipped) {
  const col = sq.charCodeAt(0) - 97;   // a=0 … h=7
  const rank = parseInt(sq[1]) - 1;    // 1=0 … 8=7
  const row  = 7 - rank;
  if (!flipped) return { row, col };
  return { row: 7 - row, col: 7 - col };
}

function renderBoard(fen) {
  const flipped = S.boardFlip;
  const { grid, turn } = parseFEN(fen);
  const board = document.getElementById('chess-board');
  board.innerHTML = '';

  for (let r = 0; r < 8; r++) {
    for (let c = 0; c < 8; c++) {
      const sq  = sqName(r, c, flipped);
      const isLight = (r + c) % 2 === 0;
      const piece = flipped ? grid[7 - r][7 - c] : grid[r][c];

      const cell = document.createElement('div');
      cell.className = `sq ${isLight ? 'light' : 'dark'}`;
      cell.dataset.sq = sq;

      if (sq === S.selected)  cell.classList.add('selected');
      if (sq === S.lastFrom || sq === S.lastTo) cell.classList.add(sq === S.lastFrom ? 'last-from' : 'last-to');

      if (piece) {
        const span = document.createElement('span');
        span.className = 'piece';
        span.textContent = PIECES[piece] || piece;
        cell.appendChild(span);
      }

      cell.addEventListener('click', () => onSquareClick(sq, piece, turn));
      board.appendChild(cell);
    }
  }

  // Board labels
  renderLabels(flipped);
}

function renderLabels(flipped) {
  const rl = document.getElementById('rank-labels');
  const fl = document.getElementById('file-labels');
  rl.innerHTML = '';
  fl.innerHTML = '';

  const ranks = flipped
    ? ['1','2','3','4','5','6','7','8']
    : ['8','7','6','5','4','3','2','1'];
  const files = flipped
    ? ['h','g','f','e','d','c','b','a']
    : ['a','b','c','d','e','f','g','h'];

  ranks.forEach(r => {
    const d = document.createElement('div');
    d.className = 'rank-label'; d.textContent = r;
    rl.appendChild(d);
  });
  files.forEach(f => {
    const d = document.createElement('div');
    d.className = 'file-label'; d.textContent = f;
    fl.appendChild(d);
  });
}

function onSquareClick(sq, piece, turn) {
  if (!S.game || S.game.status !== 'active') return;

  const myTurn = (S.myColor === 'white' && turn === 'w') ||
                 (S.myColor === 'black' && turn === 'b');
  const isMyPiece = piece &&
    ((S.myColor === 'white' && piece === piece.toUpperCase()) ||
     (S.myColor === 'black' && piece === piece.toLowerCase()));

  if (!S.selected) {
    if (!myTurn) { Toast.warn("It's not your turn! ⏳"); return; }
    if (!isMyPiece) { Toast.warn("That's not your piece! 🙈"); return; }
    S.selected = sq;
    renderBoard(S.game.fen);
    return;
  }

  if (sq === S.selected) {
    S.selected = null;
    renderBoard(S.game.fen);
    return;
  }

  // Re-select own piece
  if (isMyPiece && myTurn) {
    S.selected = sq;
    renderBoard(S.game.fen);
    return;
  }

  // Attempt move
  const move = S.selected + sq;
  sendMove(move);
}

async function sendMove(move) {
  const from = move.slice(0, 2);
  const to   = move.slice(2, 4);

  if (S.ws && S.ws.readyState === WebSocket.OPEN) {
    S.ws.send(JSON.stringify({
      type: 'move',
      data: { move, client_timestamp: Date.now() },
    }));
    S.selected = null;
    S.lastFrom = from;
    S.lastTo   = to;
  } else {
    // REST fallback
    try {
      const res = await API.makeMove(S.game.id, move);
      S.selected = null;
      S.lastFrom = from;
      S.lastTo   = to;
      await refreshGame();
    } catch(e) {
      Toast.err(`Move error: ${e.message}`);
      S.selected = null;
      renderBoard(S.game.fen);
    }
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  WebSocket
// ════════════════════════════════════════════════════════════════════════════
function connectWS(gameId) {
  if (S.ws) { S.ws.close(); S.ws = null; }
  const url = `${WS_PROTO}://${WS_HOST}/games/ws/${gameId}?token=${S.token}`;
  S.ws = new WebSocket(url);

  S.ws.onopen = () => console.log('WS connected');

  S.ws.onmessage = async (ev) => {
    const msg = JSON.parse(ev.data);
    handleWSMessage(msg);
  };

  S.ws.onerror = (e) => console.warn('WS error', e);

  S.ws.onclose = (e) => {
    console.log('WS closed', e.code);
    if (e.code === 4001) Toast.err('Session kicked: another tab opened!');
  };
}

async function handleWSMessage(msg) {
  const { type, data } = msg;

  if (type === 'connected') {
    await refreshGame();
    updateTurnBanner();
  }

  if (type === 'game_started') {
    Toast.ok('Opponent joined! Game is starting 🎉');
    await refreshGame();
    updateTurnBanner();
    if (S.game.bet_amount > 0) promptWebcam();
  }

  if (type === 'move') {
    S.game.fen = data.fen;
    S.lastFrom = data.move_uci?.slice(0,2);
    S.lastTo   = data.move_uci?.slice(2,4);
    S.selected = null;
    renderBoard(S.game.fen);
    appendMoveToList(data.move_san, data.move_number, data.player_id);
    updateTurnBanner(data);
    if (data.player_id !== S.user.id) playSound('move');
    if (data.game_over) {
      await refreshGame();
      showResult(data.result);
    }
  }

  if (type === 'game_over') {
    await refreshGame();
    showResult(data.result, data.reason);
  }

  if (type === 'penalty') {
    Toast.err(`⚠️ Penalty applied: ₹${data.amount_deducted} deducted. ${data.reason}`);
    if (data.banned) {
      Modal.show('🚫', 'Account Banned', `Your account has been banned: ${data.reason}`, 'OK').then(() => {
        S.token = null; S.user = null;
        showView('landing');
        document.getElementById('navbar').classList.remove('visible');
      });
    }
  }

  if (type === 'error') {
    Toast.err(`🚫 ${data.message}`);
    S.selected = null;
    renderBoard(S.game.fen);
  }

  if (type === 'kicked') {
    Toast.err(`You were kicked: ${data.reason}`);
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  Game helpers
// ════════════════════════════════════════════════════════════════════════════
async function refreshGame() {
  if (!S.game) return;
  try {
    S.game = await API.getGame(S.game.id);
  } catch(e) { /* ignore */ }
  renderBoard(S.game.fen);
  updatePlayerCards();
  renderMoveList();
  updatePrize();
}

function updateTurnBanner(moveData) {
  if (!S.game) return;
  const dot  = document.getElementById('turn-dot');
  const text = document.getElementById('turn-text');

  if (S.game.status === 'waiting') {
    dot.className = 'turn-dot waiting';
    text.textContent = '⏳ Waiting for an opponent to join…';
    return;
  }
  if (S.game.status === 'completed') {
    dot.className = 'turn-dot';
    text.textContent = '✅ Game finished!';
    return;
  }

  const { grid, turn } = parseFEN(S.game.fen);
  const myTurn = (S.myColor === 'white' && turn === 'w') ||
                 (S.myColor === 'black' && turn === 'b');

  if (myTurn) {
    dot.className = 'turn-dot my-turn';
    text.textContent = '🟢 Your turn! Make a move.';
  } else {
    dot.className = 'turn-dot opp-turn';
    text.textContent = '⏳ Opponent is thinking…';
  }
}

function updatePlayerCards() {
  if (!S.game) return;
  document.getElementById('prize-amount').textContent = `₹${(S.game.bet_amount*2).toFixed(2)}`;
}

function updatePrize() {
  if (!S.game) return;
  document.getElementById('prize-amount').textContent = `₹${(S.game.bet_amount*2).toFixed(2)}`;
}

function appendMoveToList(san, num, playerId) {
  const list = document.getElementById('moves-list');
  const moveNum = Math.ceil(num / 2);
  const isWhite = num % 2 === 1;

  if (isWhite) {
    const row = document.createElement('div');
    row.className = 'move-row';
    row.innerHTML = `<span class="move-num">${moveNum}.</span>
      <span class="move-san">${san}</span>`;
    row.dataset.moveNum = moveNum;
    list.appendChild(row);
  } else {
    const rows = list.querySelectorAll(`.move-row[data-move-num="${moveNum}"]`);
    const lastRow = rows[rows.length - 1];
    if (lastRow) {
      const span = document.createElement('span');
      span.className = 'move-san'; span.textContent = san;
      lastRow.appendChild(span);
    }
  }
  list.scrollTop = list.scrollHeight;
}

function renderMoveList() {
  const list = document.getElementById('moves-list');
  list.innerHTML = '';
  if (!S.game || !S.game.moves) return;
  for (const m of S.game.moves) {
    appendMoveToList(m.move_san, m.move_number, m.player_id);
  }
}

function showResult(result, reason) {
  const overlay = document.getElementById('result-overlay');
  const icon    = document.getElementById('res-icon');
  const title   = document.getElementById('res-title');
  const msg     = document.getElementById('res-msg');
  const prize   = document.getElementById('res-prize');

  let isWin = false;
  if (result === 'white' && S.myColor === 'white') isWin = true;
  if (result === 'black' && S.myColor === 'black') isWin = true;
  const isDraw = result === 'draw';

  if (isDraw) {
    icon.textContent  = '🤝';
    title.textContent = "It's a Draw!";
    msg.textContent   = 'Great game! Your bet has been refunded.';
    prize.textContent = `₹${S.game.bet_amount.toFixed(2)} refunded`;
  } else if (isWin) {
    icon.textContent  = '🏆';
    title.textContent = 'You Won!!! 🎉';
    msg.textContent   = `Amazing! You beat your opponent${reason === 'resignation' ? ' (they resigned)' : ''}!`;
    prize.textContent = `+₹${(S.game.bet_amount*2).toFixed(2)} added to wallet!`;
    spawnConfetti();
  } else {
    icon.textContent  = '😢';
    title.textContent = 'Better luck next time!';
    msg.textContent   = `You lost this game. Keep practicing — you'll win next time!`;
    prize.textContent = '';
  }

  overlay.classList.add('open');

  // Stop video recording
  if (S.video) { S.video.stop(); S.video = null; }
}

// ════════════════════════════════════════════════════════════════════════════
//  Webcam / Video Anti-Cheat
// ════════════════════════════════════════════════════════════════════════════
class VideoMonitor {
  constructor(gameId) {
    this.gameId    = gameId;
    this.stream    = null;
    this.recorder  = null;
    this.chunkNum  = 0;
    this.active    = false;
  }

  async start() {
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        video: { width: 320, height: 240, facingMode: 'user' },
        audio: false,
      });
      document.getElementById('cam-preview').srcObject = this.stream;
      document.getElementById('cam-status').textContent = '🟢 Recording – Fair play active';
      document.getElementById('start-cam-btn').style.display = 'none';
      this._startRecording();
      this.active = true;
    } catch (e) {
      document.getElementById('cam-status').textContent = '❌ Camera blocked';
      Toast.warn('⚠️ Camera access denied. This has been flagged.');
      API.reportNoCamera(this.gameId, e.message || 'Permission denied').catch(() => {});
    }
  }

  _startRecording() {
    if (!this.stream) return;
    const mimeType = MediaRecorder.isTypeSupported('video/webm;codecs=vp8')
      ? 'video/webm;codecs=vp8' : 'video/webm';

    this.recorder = new MediaRecorder(this.stream, { mimeType, videoBitsPerSecond: 500000 });
    this.recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 1000) {
        API.uploadChunk(this.gameId, e.data, this.chunkNum++)
           .catch(err => console.warn('Chunk upload failed:', err));
      }
    };
    this.recorder.start(30000);  // 30-second chunks
  }

  stop() {
    this.active = false;
    try {
      if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop();
      if (this.stream) this.stream.getTracks().forEach(t => t.stop());
    } catch(e) { /* ignore */ }
    document.getElementById('cam-preview').srcObject = null;
    document.getElementById('cam-status').textContent = 'Recording stopped';
  }
}

async function promptWebcam() {
  const confirmed = await Modal.show(
    '📹',
    'Enable Camera for Fair Play',
    `This is a real-money game (₹${S.game.bet_amount}). Fair play requires your webcam to be on during the game. Your video is saved as evidence only and is never shared publicly.`,
    'Enable Camera 📷',
    'Play Without Camera (will be flagged)',
  );
  if (confirmed) {
    S.video = new VideoMonitor(S.game.id);
    await S.video.start();
  } else {
    API.reportNoCamera(S.game.id, 'User declined camera').catch(() => {});
    Toast.warn('⚠️ No camera – this session has been flagged for review.');
  }
}

// Tab-visibility anti-cheat
document.addEventListener('visibilitychange', () => {
  if (!S.game || S.game.status !== 'active') return;
  if (document.hidden) {
    S.tabHiddenAt = Date.now();
  } else if (S.tabHiddenAt) {
    const ms = Date.now() - S.tabHiddenAt;
    S.tabHiddenAt = null;
    if (ms > 3000) {
      Toast.warn(`⚠️ You were away for ${Math.round(ms/1000)}s – this has been noted.`);
      API.reportTabHidden(S.game.id, ms).catch(() => {});
    }
  }
});

// ════════════════════════════════════════════════════════════════════════════
//  Confetti
// ════════════════════════════════════════════════════════════════════════════
function spawnConfetti() {
  const canvas = document.getElementById('confetti-canvas');
  const ctx    = canvas.getContext('2d');
  canvas.width  = window.innerWidth;
  canvas.height = window.innerHeight;

  const pieces = Array.from({length: 120}, () => ({
    x: Math.random() * canvas.width,
    y: Math.random() * -200,
    r: 6 + Math.random() * 8,
    dx: (Math.random() - .5) * 3,
    dy: 2 + Math.random() * 4,
    color: `hsl(${Math.random()*360},90%,60%)`,
    rot: Math.random() * 360,
    drot: (Math.random() - .5) * 6,
  }));

  let frame = 0;
  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (const p of pieces) {
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.rot * Math.PI / 180);
      ctx.fillStyle = p.color;
      ctx.fillRect(-p.r/2, -p.r/4, p.r, p.r/2);
      ctx.restore();
      p.x  += p.dx; p.y += p.dy;
      p.rot += p.drot;
    }
    if (++frame < 200) requestAnimationFrame(draw);
    else ctx.clearRect(0, 0, canvas.width, canvas.height);
  }
  draw();
}

function playSound(type) {
  /* Subtle click using AudioContext – no external files needed */
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const g   = ctx.createGain();
    osc.connect(g); g.connect(ctx.destination);
    osc.frequency.value = type === 'move' ? 600 : 880;
    g.gain.setValueAtTime(.2, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(.001, ctx.currentTime + .15);
    osc.start(); osc.stop(ctx.currentTime + .15);
  } catch(e) { /* ignore */ }
}

// ════════════════════════════════════════════════════════════════════════════
//  Lobby helpers
// ════════════════════════════════════════════════════════════════════════════
let _currentTab = 'waiting';

async function loadLobby() {
  await loadStats();
  await loadGames(_currentTab === 'my-games' ? null : _currentTab);
}

async function loadStats() {
  try {
    const [user, wallet] = await Promise.all([API.me(), API.getWallet()]);
    S.user   = user;
    S.wallet = wallet;
    document.getElementById('ls-played').textContent  = user.games_played;
    document.getElementById('ls-wins').textContent    = user.games_won;
    document.getElementById('ls-balance').textContent = `₹${wallet.balance.toFixed(2)}`;
    const rate = user.games_played
      ? Math.round((user.games_won / user.games_played) * 100) : 0;
    document.getElementById('ls-rate').textContent = `${rate}%`;
    document.getElementById('nav-balance').textContent = `₹${wallet.balance.toFixed(2)}`;
  } catch(e) { /* ignore */ }
}

async function loadGames(status = 'waiting') {
  const grid = document.getElementById('games-grid');
  grid.innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';
  try {
    const games = await API.getGames(status);
    grid.innerHTML = '';
    if (!games.length) {
      grid.innerHTML = '<div class="empty-state">No games here yet! 😊 Create one above!</div>';
      return;
    }
    for (const g of games) {
      const card = document.createElement('div');
      card.className = 'game-card';
      const statusClass = g.status;
      card.innerHTML = `
        <div class="gc-top">
          <span class="gc-status ${statusClass}">${g.status}</span>
          <span style="font-size:13px;color:#888">#${g.id}</span>
        </div>
        <div class="gc-player">♟ ${g.white_username}</div>
        <div class="gc-bet-label">Prize Pool</div>
        <div class="gc-bet">₹${(g.bet_amount * 2).toFixed(2)}</div>
        <div class="gc-action">
          ${g.status === 'waiting' && g.white_player_id !== S.user?.id
            ? `<button class="btn btn-primary btn-block" onclick="joinGame(${g.id})">Join Game ♟</button>`
            : g.status === 'active'
            ? `<button class="btn btn-secondary btn-block" onclick="watchGame(${g.id})">Watch 👀</button>`
            : `<button class="btn btn-ghost btn-block" onclick="viewGame(${g.id})">View ♟</button>`
          }
        </div>`;
      grid.appendChild(card);
    }
  } catch(e) {
    grid.innerHTML = `<div class="empty-state">❌ Failed to load games: ${e.message}</div>`;
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  Game actions
// ════════════════════════════════════════════════════════════════════════════
async function createGame() {
  if (!S.wallet || S.wallet.balance <= 0) {
    const ok = await Modal.show('💰', 'No Balance!',
      'You need to add money to your wallet first before creating a game.',
      'Go to Wallet', 'Cancel');
    if (ok) { showView('wallet'); loadWallet(); }
    return;
  }

  const available = S.wallet.balance - S.wallet.total_invested;
  if (available <= 0) {
    Toast.warn('All your balance is locked in active games!');
    return;
  }

  // Build bet options dynamically
  const amounts = [1, 5, 10, 25, 50, 100].filter(a => a <= available);
  const overlayEl = document.getElementById('modal-overlay');
  const boxEl     = document.getElementById('modal-box');

  document.getElementById('modal-icon').textContent  = '♟';
  document.getElementById('modal-title').textContent = 'Create a Game';
  document.getElementById('modal-body').innerHTML = `
    <p style="margin-bottom:14px;color:#7578A8">Choose your bet amount. The winner gets double! Available: <strong>₹${available.toFixed(2)}</strong></p>
    <div style="display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-bottom:16px">
      ${amounts.map(a => `<button class="quick-chip" onclick="document.getElementById('bet-input').value=${a};document.querySelectorAll('.quick-chip').forEach(c=>c.classList.remove('active'));this.classList.add('active')" style="min-width:70px">₹${a}</button>`).join('')}
    </div>
    <input class="field-input" type="number" id="bet-input" placeholder="Or type amount (₹1-₹${Math.min(100,available).toFixed(0)})" min="1" max="${Math.min(100,available)}" step="1" style="margin-bottom:8px" />
    <small style="color:#7578A8;display:block;text-align:center">💡 Max ₹100 per bet</small>`;
  document.getElementById('modal-cancel').textContent = 'Cancel';
  document.getElementById('modal-ok').textContent     = 'Create Game! ♟';
  overlayEl.classList.add('open');

  Modal._cb = async (ok) => {
    Modal._cb = null;
    overlayEl.classList.remove('open');
    if (!ok) return;
    const amt = parseFloat(document.getElementById('bet-input').value);
    if (!amt || amt < 1) { Toast.err('Enter a valid bet amount!'); return; }
    try {
      const game = await API.createGame(amt);
      Toast.ok(`Game created! Waiting for opponent… (Game #${game.id})`);
      enterGame(game, 'white');
    } catch(e) {
      Toast.err(`Couldn't create game: ${e.message}`);
    }
  };
}

async function joinGame(gameId) {
  const confirmed = await Modal.show('♟', 'Join this Game?',
    'Are you sure you want to join? The bet amount will be locked from your wallet.',
    'Join Game! 🎮', 'Cancel');
  if (!confirmed) return;
  try {
    const game = await API.joinGame(gameId);
    Toast.ok('You joined the game! Good luck! 🎉');
    enterGame(game, 'black');
  } catch(e) {
    Toast.err(`Couldn't join: ${e.message}`);
  }
}

async function watchGame(gameId) {
  try {
    const game = await API.getGame(gameId);
    S.game = game;
    S.myColor = null;
    S.boardFlip = false;
    showView('game');
    renderBoard(game.fen);
    renderMoveList();
    updatePrize();
    document.getElementById('turn-text').textContent = '👀 Watching this game';
  } catch(e) {
    Toast.err(e.message);
  }
}

async function viewGame(gameId) {
  await watchGame(gameId);
}

async function enterGame(game, myColor) {
  S.game    = game;
  S.myColor = myColor;
  S.boardFlip = myColor === 'black';
  S.selected  = null;
  S.lastFrom = S.lastTo = null;

  showView('game');
  renderBoard(game.fen);
  renderMoveList();
  updatePrize();
  updateTurnBanner();

  // Player names
  document.getElementById('my-name').textContent  = S.user.username + ' (You)';
  document.getElementById('my-ava').textContent   = myColor === 'white' ? '♔' : '♚';

  if (game.status === 'active') {
    const oppId = myColor === 'white' ? game.black_player_id : game.white_player_id;
    if (oppId) {
      try {
        const opp = await API.getUser(oppId);
        document.getElementById('opp-name').textContent = opp.username;
      } catch(e) { /* ignore */ }
    }
  }

  // WebSocket
  connectWS(game.id);

  // Webcam for bet games that are already active
  if (game.status === 'active' && game.bet_amount > 0) {
    promptWebcam();
  }

  // Share link
  document.getElementById('copy-game-link').onclick = () => {
    navigator.clipboard?.writeText(location.origin + `?join=${game.id}`)
      .then(() => Toast.ok('Invite link copied! Share it with your friend 🔗'))
      .catch(() => Toast.warn('Could not copy – share game ID: #' + game.id));
  };
}

// ════════════════════════════════════════════════════════════════════════════
//  Wallet
// ════════════════════════════════════════════════════════════════════════════
let _qrInstance = null;

async function loadWallet() {
  try {
    const [wallet, txs, payouts, deposits] = await Promise.all([
      API.getWallet(), API.transactions(), API.myPayouts(), API.myDepositReqs(),
    ]);
    S.wallet = wallet;

    document.getElementById('w-balance').textContent = `₹${wallet.balance.toFixed(2)}`;
    document.getElementById('w-locked').textContent  = `₹${wallet.total_invested.toFixed(2)}`;
    const pct = Math.min(100, (wallet.balance / 100) * 100);
    document.getElementById('w-progress').style.width = `${pct}%`;
    document.getElementById('nav-balance').textContent = `₹${wallet.balance.toFixed(2)}`;

    // Pending payouts list
    const pList = document.getElementById('payouts-list');
    const pendingPayouts = payouts.filter(p => p.status === 'pending');
    if (!pendingPayouts.length) {
      pList.innerHTML = '<div style="font-size:13px;color:#7578A8">No pending payouts 🎉</div>';
    } else {
      pList.innerHTML = pendingPayouts.map(p => {
        const release = p.auto_release_at ? new Date(p.auto_release_at) : null;
        const hoursLeft = release ? Math.max(0, ((release - Date.now()) / 3600000)).toFixed(1) : '?';
        return `<div class="tx-item" style="margin-bottom:8px">
          <div class="tx-icon">⏳</div>
          <div class="tx-desc">
            <div class="tx-desc-title">Game #${p.game_id} winnings</div>
            <div class="tx-desc-date">Review in ~${hoursLeft}h · Platform fee: ₹${p.platform_fee.toFixed(2)}</div>
          </div>
          <div class="tx-amount pos">₹${p.net_amount.toFixed(2)}</div>
        </div>`;
      }).join('');
    }

    // My deposit requests
    const dList = document.getElementById('my-deposits-list');
    if (!deposits.length) {
      dList.innerHTML = '<div class="empty-state">No deposit requests yet</div>';
    } else {
      const depIcons = { pending:'⏳', approved:'✅', rejected:'❌' };
      dList.innerHTML = deposits.map(d => `
        <div class="tx-item">
          <div class="tx-icon">${depIcons[d.status]||'💵'}</div>
          <div class="tx-desc">
            <div class="tx-desc-title">₹${d.amount.toFixed(2)} – UTR: ${d.utr_number}</div>
            <div class="tx-desc-date">${new Date(d.created_at).toLocaleString('en-IN')}${d.rejection_reason ? ' · Rejected: ' + d.rejection_reason : ''}</div>
          </div>
          <div class="tx-amount" style="font-size:13px;font-weight:800;color:${d.status==='approved'?'#43C59E':d.status==='rejected'?'#FF5252':'#FF9800'}">${d.status.toUpperCase()}</div>
        </div>`).join('');
    }

    // Transactions
    const list = document.getElementById('tx-list');
    if (!txs.length) {
      list.innerHTML = '<div class="empty-state">No transactions yet 😊</div>';
    } else {
      list.innerHTML = txs.map(tx => {
        const isPos = ['deposit','win','refund'].includes(tx.type);
        const icons = { deposit:'💵', withdrawal:'🏦', bet:'♟', win:'🏆', refund:'↩️' };
        return `
          <div class="tx-item">
            <div class="tx-icon">${icons[tx.type]||'💸'}</div>
            <div class="tx-desc">
              <div class="tx-desc-title">${tx.description || tx.type}</div>
              <div class="tx-desc-date">${new Date(tx.created_at).toLocaleString('en-IN')}</div>
            </div>
            <div class="tx-amount ${isPos?'pos':'neg'}">${isPos?'+':'-'}₹${Math.abs(tx.amount).toFixed(2)}</div>
          </div>`;
      }).join('');
    }
  } catch(e) {
    Toast.err('Could not load wallet: ' + e.message);
  }
}

async function showUPIQR() {
  const amt = parseFloat(document.getElementById('deposit-amount').value);
  if (!amt || amt < 10) { Toast.err('Minimum deposit is ₹10'); return; }
  if (amt > 100)        { Toast.err('Maximum deposit is ₹100'); return; }

  try {
    const info = await API.upiInfo(amt);
    document.getElementById('qr-section').style.display = 'block';
    document.getElementById('upi-id-display').textContent = `UPI ID: ${info.upi_id}`;

    // Generate QR code
    const qrEl = document.getElementById('qr-code');
    qrEl.innerHTML = '';
    if (window.QRCode) {
      new QRCode(qrEl, {
        text: info.upi_url,
        width: 200, height: 200,
        colorDark: '#000000', colorLight: '#ffffff',
        correctLevel: QRCode.CorrectLevel.M,
      });
    } else {
      qrEl.innerHTML = `<div style="font-size:13px;padding:20px;color:#333">UPI ID: <strong>${info.upi_id}</strong><br/>Amount: <strong>₹${amt}</strong></div>`;
    }
    document.getElementById('utr-input').focus();
    Toast.ok(`Pay ₹${amt} to ${info.upi_id} and enter UTR below`);
  } catch(e) {
    Toast.err('Could not load UPI info: ' + e.message);
  }
}

async function submitUTR() {
  const amt = parseFloat(document.getElementById('deposit-amount').value);
  const utr = document.getElementById('utr-input').value.trim();
  if (!utr || utr.length < 6) { Toast.err('Please enter a valid UTR number (6+ characters)'); return; }
  if (!amt || amt < 10)       { Toast.err('Invalid amount'); return; }

  try {
    const r = await API.submitDeposit(amt, utr);
    Toast.ok(`✅ Deposit request submitted! UTR: ${r.utr_number}. Admin will verify shortly.`);
    document.getElementById('qr-section').style.display = 'none';
    document.getElementById('deposit-amount').value = '';
    document.getElementById('utr-input').value = '';
    loadWallet();
  } catch(e) {
    Toast.err(e.message);
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  Profile
// ════════════════════════════════════════════════════════════════════════════
async function loadProfile() {
  try {
    const [user, flags] = await Promise.all([API.me(), API.myFlags()]);
    S.user = user;
    document.getElementById('p-avatar').textContent   = user.username[0].toUpperCase();
    document.getElementById('p-username').textContent = user.username;
    document.getElementById('p-email').textContent    = user.email;
    document.getElementById('p-since').textContent    = 'Joined: ' + new Date(user.created_at).toLocaleDateString('en-IN');
    document.getElementById('p-played').textContent   = user.games_played;
    document.getElementById('p-wins').textContent     = user.games_won;
    document.getElementById('p-earned').textContent   = `₹${user.total_earned.toFixed(2)}`;
    const rate = user.games_played
      ? Math.round((user.games_won / user.games_played) * 100) : 0;
    document.getElementById('p-rate').textContent = `${rate}%`;
    document.getElementById('nav-avatar').textContent = user.username[0].toUpperCase();

    if (flags.length) {
      document.getElementById('p-flags-wrap').style.display = 'block';
      document.getElementById('p-flags').innerHTML = flags.map(f => `
        <div class="tx-item">
          <div class="tx-icon">${f.severity===3?'🚫':f.severity===2?'⚠️':'⚡'}</div>
          <div class="tx-desc">
            <div class="tx-desc-title">${f.flag_type}: ${f.description}</div>
            <div class="tx-desc-date">${new Date(f.created_at).toLocaleString('en-IN')}</div>
          </div>
          <div class="tx-amount neg">Severity ${f.severity}</div>
        </div>`).join('');
    }
  } catch(e) {
    Toast.err('Could not load profile: ' + e.message);
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  Auth
// ════════════════════════════════════════════════════════════════════════════
function afterLogin(token, user) {
  S.token = token;
  S.user  = user;
  localStorage.setItem('cw_token', token);
  document.getElementById('nav-avatar').textContent = user.username[0].toUpperCase();
  document.getElementById('navbar').classList.add('visible');
  showView('lobby');
  loadLobby();
}

function logout() {
  S.token = S.user = S.game = S.wallet = null;
  if (S.ws)    { S.ws.close(); S.ws = null; }
  if (S.video) { S.video.stop(); S.video = null; }
  localStorage.removeItem('cw_token');
  document.getElementById('navbar').classList.remove('visible');
  showView('landing');
}

// ════════════════════════════════════════════════════════════════════════════
//  Landing preview board
// ════════════════════════════════════════════════════════════════════════════
function buildPreviewBoard() {
  const el = document.getElementById('preview-board');
  if (!el) return;
  const startFEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR';
  const grid = [];
  for (const rank of startFEN.split('/')) {
    const row = [];
    for (const ch of rank) {
      if ('12345678'.includes(ch)) for (let i=0;i<+ch;i++) row.push(null);
      else row.push(ch);
    }
    grid.push(row);
  }
  el.innerHTML = '';
  for (let r=0; r<8; r++) {
    for (let c=0; c<8; c++) {
      const d = document.createElement('div');
      d.style.cssText = `width:42px;height:42px;display:flex;align-items:center;justify-content:center;`;
      d.style.background = (r+c)%2===0 ? '#F0D9B5' : '#B58863';
      if (grid[r][c]) {
        const s = document.createElement('span');
        s.style.cssText = 'font-size:28px;text-shadow:0 1px 3px rgba(0,0,0,.3)';
        s.textContent = PIECES[grid[r][c]] || '';
        d.appendChild(s);
      }
      el.appendChild(d);
    }
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  Global App object (exposed for HTML onclick attributes)
// ════════════════════════════════════════════════════════════════════════════
const App = {
  goLobby() {
    document.getElementById('result-overlay').classList.remove('open');
    if (S.ws) { S.ws.close(); S.ws = null; }
    if (S.video) { S.video.stop(); S.video = null; }
    S.game = null;
    showView('lobby');
    loadLobby();
  },
};
window.App = App;
window.showView = showView;
window.joinGame = joinGame;
window.watchGame = watchGame;
window.viewGame = viewGame;

// ════════════════════════════════════════════════════════════════════════════
//  Event Listeners
// ════════════════════════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', async () => {
  buildPreviewBoard();

  // ── Auth forms ──
  document.getElementById('register-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type=submit]');
    btn.disabled = true; btn.textContent = 'Creating account… ⏳';
    try {
      const user = await API.register(
        document.getElementById('reg-username').value.trim(),
        document.getElementById('reg-email').value.trim(),
        document.getElementById('reg-password').value,
      );
      // Auto-login (/auth/me needs Bearer token — set it only after login)
      const data = await API.login(
        document.getElementById('reg-username').value.trim(),
        document.getElementById('reg-password').value,
      );
      afterLogin(data.access_token, user);
      Toast.ok('Welcome to ChessWager! 🎉');
    } catch(e) {
      Toast.err(e.message);
    } finally {
      btn.disabled = false; btn.textContent = 'Create Account! 🚀';
    }
  });

  document.getElementById('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type=submit]');
    btn.disabled = true; btn.textContent = 'Logging in… ⏳';
    try {
      const data = await API.login(
        document.getElementById('login-username').value.trim(),
        document.getElementById('login-password').value,
      );
      afterLogin(data.access_token, data.user);
      Toast.ok(`Welcome back, ${user.username}! ♟`);
    } catch(e) {
      Toast.err(e.message);
    } finally {
      btn.disabled = false; btn.textContent = 'Let\'s Play! 🎮';
    }
  });

  // ── Logout ──
  document.getElementById('logout-btn').addEventListener('click', async () => {
    const ok = await Modal.show('👋', 'Logging out?', 'Are you sure you want to leave?', 'Yes, logout', 'Stay');
    if (ok) logout();
  });

  // ── Nav links ──
  document.querySelectorAll('.nav-link').forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      const view = link.dataset.view;
      showView(view);
      if (view === 'lobby')   loadLobby();
      if (view === 'wallet')  loadWallet();
      if (view === 'profile') loadProfile();
    });
  });

  // ── Lobby tabs ──
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      _currentTab = tab.dataset.tab;
      loadGames(_currentTab === 'my-games' ? null : _currentTab);
    });
  });

  // ── Create game ──
  document.getElementById('create-game-btn').addEventListener('click', createGame);

  // ── Modal buttons ──
  document.getElementById('modal-ok').addEventListener('click', () => Modal.close(true));
  document.getElementById('modal-cancel').addEventListener('click', () => Modal.close(false));

  // ── Webcam start button ──
  document.getElementById('start-cam-btn').addEventListener('click', async () => {
    if (!S.video) S.video = new VideoMonitor(S.game?.id || 0);
    await S.video.start();
  });

  // ── Resign ──
  document.getElementById('resign-btn').addEventListener('click', async () => {
    if (!S.game || S.game.status !== 'active') return;
    const ok = await Modal.show('🏳️', 'Resign the Game?',
      'Are you sure? Your opponent will win and receive the prize money.',
      'Yes, I resign', 'Keep playing');
    if (!ok) return;

    if (S.ws && S.ws.readyState === WebSocket.OPEN) {
      S.ws.send(JSON.stringify({ type: 'resign' }));
    } else {
      try {
        await API.resign(S.game.id);
        showResult(S.myColor === 'white' ? 'black' : 'white', 'resignation');
      } catch(e) {
        Toast.err(e.message);
      }
    }
  });

  // ── Wallet quick chips (pre-fill amount and generate QR) ──
  document.querySelectorAll('.quick-chip').forEach(chip => {
    chip.addEventListener('click', async () => {
      document.querySelectorAll('.quick-chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      document.getElementById('deposit-amount').value = chip.dataset.amount;
      await showUPIQR();
    });
  });

  document.getElementById('show-qr-btn').addEventListener('click', showUPIQR);
  document.getElementById('submit-utr-btn').addEventListener('click', submitUTR);

  document.getElementById('withdraw-btn').addEventListener('click', async () => {
    const amt = parseFloat(document.getElementById('withdraw-amount').value);
    if (!amt || amt < 1) { Toast.err('Enter a valid amount'); return; }
    try {
      await API.withdraw(amt);
      Toast.ok(`₹${amt.toFixed(2)} withdrawn! 🏦`);
      loadWallet();
    } catch(e) { Toast.err(e.message); }
  });

  // ── Auto-login from saved token ──
  const saved = localStorage.getItem('cw_token');
  if (saved) {
    S.token = saved;
    try {
      const user = await API.me();
      afterLogin(saved, user);
    } catch(e) {
      localStorage.removeItem('cw_token');
      S.token = null;
    }
  }

  // ── Deep-link: ?join=gameId ──
  const params = new URLSearchParams(location.search);
  if (params.has('join') && S.token) {
    const gid = parseInt(params.get('join'));
    history.replaceState({}, '', '/');
    if (gid) joinGame(gid);
  }
});
