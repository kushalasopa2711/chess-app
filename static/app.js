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
  wsGameId:   null,       // game the current WS belongs to
  wsPingTimer:null,       // setInterval id for WS heartbeat
  wsReconnectTimer: null, // pending reconnect setTimeout id
  wsClosedManually: false,// suppress auto-reconnect on intentional close
  selected:   null,       // selected square name (e.g. 'e2')
  legalTargets: [],       // server-confirmed legal destinations for S.selected
  lastFrom:   null,
  lastTo:     null,
  boardFlip:  false,      // true when playing as black
  video:      null,       // VideoMonitor instance
  tabHiddenAt: null,
  clockTimer: null,       // setInterval id for live clock display
  clock:      { whiteMs: 0, blackMs: 0, turn: 'w', syncAt: 0 },
  movePending: false,     // true while a sent move awaits server confirmation
  movePendingTimer: null, // safety timeout to release the lock if WS hiccups
  userWs:     null,       // per-user notification WebSocket
  userWsReconnectTimer: null,
  wsReconnectBurst: 0,   // exponential backoff for game WS reconnect
  wsLastDropToast: 0,     // throttle "connection dropped" toasts
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
  // All API calls share a hard timeout so a stuck/cold-starting backend never
  // hangs the UI forever (e.g. resign sitting on "Resigning…").
  async req(method, path, body, isForm = false, timeoutMs = 22000) {
    const headers = {};
    if (S.token) headers['Authorization'] = `Bearer ${S.token}`;
    if (body && !isForm) headers['Content-Type'] = 'application/json';
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    let res;
    try {
      res = await fetch(BASE + path, {
        method,
        headers,
        body: isForm ? body : (body ? JSON.stringify(body) : undefined),
        signal: ctrl.signal,
      });
    } catch (e) {
      clearTimeout(t);
      if (e && e.name === 'AbortError') {
        throw new Error('Request timed out — slow network or busy server. Please try again.');
      }
      const raw = (e && e.message) ? String(e.message) : '';
      if (/failed to fetch|networkerror|load failed|internet|offline/i.test(raw)) {
        throw new Error('Could not reach the server right now. If you are on Wi‑Fi, try again in a moment (the app will keep reconnecting during play).');
      }
      throw new Error(raw || 'Temporary network issue — please try again.');
    }
    clearTimeout(t);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const det = data.detail;
      const msg = typeof det === 'string' ? det
        : Array.isArray(det) ? det.map((x) => x.msg || JSON.stringify(x)).join('; ')
        : `HTTP ${res.status}`;
      throw new Error(msg);
    }
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
  withdraw:        (amt, upi) => API.post('/wallet/withdraw', { amount: amt, destination_upi: upi }),
  myWithdrawals:   ()       => API.get('/wallet/my-withdrawals'),
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
  createGame:      (opts)   => API.post('/games', opts),
  getGame:         (id)     => API.get(`/games/${id}`),
  legalMoves:      (id, from) => API.get(`/games/${id}/legal-moves?from=${encodeURIComponent(from)}`),
  joinGame:        (id)     => API.post(`/games/${id}/join`),
  cancelGame:      (id)     => API.post(`/games/${id}/cancel`, {}),
  makeMove:        (id, mv) => API.req(
    'POST', `/games/${id}/move`,
    { move: mv, client_timestamp: Date.now() },
    false, 15000,
  ),
  resign:          (id)     => API.req('POST', `/games/${id}/resign`, undefined, false, 8000),
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
    // Large WebM slices on slow mobile networks need a generous ceiling.
    return API.req('POST', `/video/${id}/chunk`, fd, true, 120000);
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
      const mb = document.getElementById('modal-body');
      mb.classList.remove('modal-body--form');
      mb.textContent = body;
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

  // Scroll to the top of the NEW view, hard (no smooth-scroll animation —
  // smooth-scroll mid-layout-change was leaving users mid-page after login).
  // Use rAF to wait for the new view to be laid out, then jump.
  requestAnimationFrame(() => {
    const html = document.documentElement;
    const prev = html.style.scrollBehavior;
    html.style.scrollBehavior = 'auto';
    window.scrollTo(0, 0);
    if (document.body) document.body.scrollTop = 0;
    html.scrollTop = 0;
    if (v && typeof v.scrollIntoView === 'function') {
      v.scrollIntoView({ block: 'start', inline: 'nearest' });
    }
    html.style.scrollBehavior = prev || '';
  });
}

// ════════════════════════════════════════════════════════════════════════════
//  Chess clock (client-side smooth display — server sends authoritative ms)
// ════════════════════════════════════════════════════════════════════════════
function formatClock(ms) {
  ms = Math.max(0, Math.floor(ms));
  const s = Math.ceil(ms / 1000);
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${r.toString().padStart(2, '0')}`;
}

function syncClockFromGame() {
  if (!S.game || S.game.status !== 'active') return;
  const { turn } = parseFEN(S.game.fen);
  S.clock.whiteMs = S.game.white_time_ms ?? 600000;
  S.clock.blackMs = S.game.black_time_ms ?? 600000;
  S.clock.turn = turn;
  S.clock.syncAt = Date.now();
  updateClockDisplay();
}

function updateClockDisplay() {
  const wEl = document.getElementById('clock-white-val');
  const bEl = document.getElementById('clock-black-val');
  const wBox = document.getElementById('clock-white');
  const bBox = document.getElementById('clock-black');
  if (!wEl || !bEl) return;

  const elapsed = Date.now() - S.clock.syncAt;
  let w = S.clock.whiteMs;
  let b = S.clock.blackMs;
  if (S.clock.turn === 'w') w = Math.max(0, w - elapsed);
  else b = Math.max(0, b - elapsed);

  wEl.textContent = formatClock(w);
  bEl.textContent = formatClock(b);

  const low = 30000;
  wBox.classList.toggle('clock-low', w < low);
  bBox.classList.toggle('clock-low', b < low);
  wBox.classList.toggle('clock-active', S.clock.turn === 'w');
  bBox.classList.toggle('clock-active', S.clock.turn === 'b');
}

function startClockTicker() {
  stopClockTicker();
  S.clockTimer = setInterval(updateClockDisplay, 200);
}

function stopClockTicker() {
  if (S.clockTimer) {
    clearInterval(S.clockTimer);
    S.clockTimer = null;
  }
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
      if (S.legalTargets && S.legalTargets.includes(sq)) {
        cell.classList.add(piece ? 'legal-capture' : 'legal-target');
      }

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

function clearSelection() {
  S.selected = null;
  S.legalTargets = [];
}

async function selectPiece(sq) {
  S.selected = sq;
  S.legalTargets = [];
  renderBoard(S.game.fen);
  // Ask the server which squares are actually legal so we can highlight
  // them and reject illegal clicks BEFORE they hit the server.
  if (!S.game) return;
  const gameId = S.game.id;
  try {
    const r = await API.legalMoves(gameId, sq);
    // Make sure the selection is still relevant (user may have clicked away).
    if (S.selected !== sq || !S.game || S.game.id !== gameId) return;
    S.legalTargets = Array.isArray(r.to) ? r.to : [];
    renderBoard(S.game.fen);
  } catch (_e) {
    // Non-fatal — board is still usable, server will reject illegal moves.
  }
}

function onSquareClick(sq, piece, _turnAtRender) {
  if (!S.game || S.game.status !== 'active') return;
  if (S.movePending) {
    Toast.warn("Sending your move… ⏳");
    return;
  }

  // Always read whose turn it is from the *current* FEN, not the closure-captured
  // value at the moment of render. Otherwise stale clicks racing with the server
  // produce a confusing "It is not your turn." error.
  const { turn } = parseFEN(S.game.fen);
  const myTurn = (S.myColor === 'white' && turn === 'w') ||
                 (S.myColor === 'black' && turn === 'b');
  const isMyPiece = piece &&
    ((S.myColor === 'white' && piece === piece.toUpperCase()) ||
     (S.myColor === 'black' && piece === piece.toLowerCase()));

  if (!S.selected) {
    if (!myTurn) { Toast.warn("It's not your turn! ⏳"); return; }
    if (!isMyPiece) { Toast.warn("That's not your piece! 🙈"); return; }
    selectPiece(sq);
    return;
  }

  // Clicking the already-selected square deselects.
  if (sq === S.selected) {
    clearSelection();
    renderBoard(S.game.fen);
    return;
  }

  // Re-select one of our own pieces.
  if (isMyPiece && myTurn) {
    selectPiece(sq);
    return;
  }

  // Reject clicks on squares we *know* are not legal — eliminates the
  // "Illegal move" toast spam for ordinary user misclicks.
  if (S.legalTargets && S.legalTargets.length && !S.legalTargets.includes(sq)) {
    Toast.warn('That square is not a legal move for the selected piece.');
    return;
  }

  // Attempt move. Promotion: auto-queen for now.
  let move = S.selected + sq;
  if (needsPromotion(S.selected, sq, piece)) move += 'q';
  sendMove(move);
}

function needsPromotion(from, to, _targetPiece) {
  // Detect pawn promotion using the FROM piece on the live board.
  const { grid } = parseFEN(S.game.fen);
  const file = from.charCodeAt(0) - 97;
  const rank = parseInt(from[1], 10);   // 1..8
  const row  = 8 - rank;                 // grid row
  const p = grid[row]?.[file];
  if (!p) return false;
  if (p === 'P' && to[1] === '8') return true;
  if (p === 'p' && to[1] === '1') return true;
  return false;
}

function clearMoveLock() {
  S.movePending = false;
  if (S.movePendingTimer) {
    clearTimeout(S.movePendingTimer);
    S.movePendingTimer = null;
  }
}

async function sendMoveViaREST(move) {
  try {
    const res = await API.makeMove(S.game.id, move);
    clearMoveLock();
    if (res.time_forfeit) {
      await refreshGame();
      showResult(res.result, 'time_forfeit');
      return;
    }
    await refreshGame();
  } catch (e) {
    clearMoveLock();
    Toast.err(e.message || 'Could not send move');
    if (S.game) renderBoard(S.game.fen);
  }
}

async function sendMove(move) {
  if (S.movePending) return;
  if (!S.game || S.game.status !== 'active') return;

  S.movePending = true;
  clearSelection();
  // Don't pre-paint the destination — show a subtle "Sending move…" hint on
  // the turn banner instead. The board re-renders on the authoritative ack.
  const banner = document.getElementById('turn-text');
  const dot    = document.getElementById('turn-dot');
  if (dot) dot.className = 'turn-dot waiting';
  if (banner) banner.textContent = '⏳ Sending your move…';
  renderBoard(S.game.fen);

  // Optimistically hand the clock to the opponent so their timer starts
  // ticking right away. The next authoritative server message will overwrite.
  try {
    const myColLetter = S.myColor === 'white' ? 'w' : (S.myColor === 'black' ? 'b' : null);
    if (myColLetter) {
      S.clock.turn = myColLetter === 'w' ? 'b' : 'w';
      S.clock.syncAt = Date.now();
      updateClockDisplay();
    }
  } catch (_e) { /* non-critical */ }

  // Safety: if no ack within 12s, re-sync from the server (don't blindly
  // re-send — re-sending caused the spurious "Not your turn" 400s when the
  // first attempt actually got through over WS).
  if (S.movePendingTimer) clearTimeout(S.movePendingTimer);
  S.movePendingTimer = setTimeout(async () => {
    if (!S.movePending) return;
    clearMoveLock();
    try { await refreshGame(); } catch (_e) { /* ignore */ }
    Toast.warn('Server was slow — board re-synced. Try again if needed.');
  }, 12000);

  if (S.ws && S.ws.readyState === WebSocket.OPEN) {
    try {
      S.ws.send(JSON.stringify({
        type: 'move',
        data: { move, client_timestamp: Date.now() },
      }));
      return;
    } catch (_e) {
      // WS send failed mid-flight; fall through to REST below.
    }
  }

  // WS closed/closing/unavailable — use REST directly.
  await sendMoveViaREST(move);
}

// ════════════════════════════════════════════════════════════════════════════
//  WebSocket  (heartbeat + auto-reconnect)
// ════════════════════════════════════════════════════════════════════════════
function stopWSHeartbeat() {
  if (S.wsPingTimer) { clearInterval(S.wsPingTimer); S.wsPingTimer = null; }
}

function startWSHeartbeat() {
  stopWSHeartbeat();
  S.wsPingTimer = setInterval(() => {
    if (S.ws && S.ws.readyState === WebSocket.OPEN) {
      try { S.ws.send(JSON.stringify({ type: 'ping' })); } catch (_e) { /* ignore */ }
    }
  }, 25000);
}

function setConnectionBanner(text, cls) {
  // Soft, non-blocking indicator inside the turn banner so the user knows
  // the live channel is reconnecting.
  const dot  = document.getElementById('turn-dot');
  const tEl  = document.getElementById('turn-text');
  if (dot && cls) dot.className = `turn-dot ${cls}`;
  if (tEl && text) tEl.textContent = text;
}

function closeWS({ manual = false } = {}) {
  S.wsClosedManually = manual;
  stopWSHeartbeat();
  if (S.wsReconnectTimer) { clearTimeout(S.wsReconnectTimer); S.wsReconnectTimer = null; }
  if (S.ws) {
    try { S.ws.close(); } catch (_e) { /* ignore */ }
    S.ws = null;
  }
  S.wsGameId = null;
}

function connectWS(gameId) {
  if (S.wsReconnectTimer) { clearTimeout(S.wsReconnectTimer); S.wsReconnectTimer = null; }
  if (S.ws) { try { S.ws.close(); } catch (_e) {} S.ws = null; }
  S.wsClosedManually = false;
  if (S.wsGameId != null && S.wsGameId !== gameId) {
    S.wsReconnectBurst = 0;
  }
  S.wsGameId = gameId;

  const url = `${WS_PROTO}://${WS_HOST}/games/ws/${gameId}?token=${S.token}`;
  let ws;
  try {
    ws = new WebSocket(url);
  } catch (e) {
    console.warn('WS open failed', e);
    scheduleWSReconnect(gameId);
    return;
  }
  S.ws = ws;

  ws.onopen = () => {
    console.log('WS connected', gameId);
    S.wsReconnectBurst = 0;
    startWSHeartbeat();
    // Re-sync state in case we missed events while disconnected.
    refreshGame().then(updateTurnBanner).catch(() => {});
  };

  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_e) { return; }
    handleWSMessage(msg);
  };

  ws.onerror = (e) => console.warn('WS error', e);

  ws.onclose = (e) => {
    console.log('WS closed', e.code, e.reason);
    stopWSHeartbeat();
    S.ws = null;
    if (e.code === 4001) {
      Toast.err('Session kicked: another tab opened!');
      return;
    }
    if (S.wsClosedManually) return;
    // Auto-reconnect if the game is still active and we still want this one.
    if (S.game && S.game.id === gameId && S.game.status === 'active') {
      const now = Date.now();
      if (now - (S.wsLastDropToast || 0) > 28000) {
        S.wsLastDropToast = now;
        Toast.warn('Live connection dropped — reconnecting… (moves still work via backup sync)');
      }
      scheduleWSReconnect(gameId);
    }
  };
}

function scheduleWSReconnect(gameId) {
  if (S.wsReconnectTimer) clearTimeout(S.wsReconnectTimer);
  S.wsReconnectBurst = (S.wsReconnectBurst || 0) + 1;
  const delayMs = Math.min(45000, Math.round(2000 * Math.pow(1.65, S.wsReconnectBurst - 1)));
  S.wsReconnectTimer = setTimeout(() => {
    S.wsReconnectTimer = null;
    if (!S.game || S.game.id !== gameId || S.game.status !== 'active') return;
    if (S.ws && S.ws.readyState === WebSocket.OPEN) return;
    connectWS(gameId);
  }, delayMs);
}

async function handleWSMessage(msg) {
  const { type, data } = msg;

  if (type === 'connected') {
    await refreshGame();
    updateTurnBanner();
  }

  if (type === 'game_started') {
    const vsCpu = data.is_vs_cpu;
    Toast.ok(vsCpu ? 'Playing versus computer! You have White ♟' : 'Opponent joined! Game is starting 🎉');
    await refreshGame();
    updateTurnBanner();
    if (S.game.bet_amount > 0 && S.myColor) promptWebcam();
  }

  if (type === 'move') {
    // Server confirmed someone's move — if it was ours, release the input lock.
    if (data.player_id === S.user?.id) clearMoveLock();
    S.game.fen = data.fen;
    S.lastFrom = data.move_uci?.slice(0,2);
    S.lastTo   = data.move_uci?.slice(2,4);
    clearSelection();
    renderBoard(S.game.fen);
    appendMoveToList(data.move_san, data.move_number, data.player_id);
    if (data.white_time_ms != null && data.black_time_ms != null) {
      S.clock.whiteMs = data.white_time_ms;
      S.clock.blackMs = data.black_time_ms;
      S.clock.turn = parseFEN(data.fen).turn;
      S.clock.syncAt = Date.now();
      updateClockDisplay();
    }
    updateTurnBanner(data);
    if (data.player_id !== S.user?.id) playSound('move');
    if (data.game_over) {
      await refreshGame();
    }
  }

  if (type === 'game_over') {
    clearMoveLock();
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
    clearMoveLock();
    clearSelection();
    S.lastFrom = S.lastTo = null;
    if (S.game) renderBoard(S.game.fen);
    updateTurnBanner();
    Toast.err(`🚫 ${data.message}`);
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
  updateTurnBanner();
  if (S.game.status === 'active') {
    syncClockFromGame();
    startClockTicker();
  } else {
    stopClockTicker();
    const wEl = document.getElementById('clock-white-val');
    const bEl = document.getElementById('clock-black-val');
    if (wEl) wEl.textContent = '—';
    if (bEl) bEl.textContent = '—';
  }
}

function updateTurnBanner(moveData) {
  if (!S.game) return;
  const dot  = document.getElementById('turn-dot');
  const text = document.getElementById('turn-text');

  if (S.game.status === 'waiting') {
    dot.className = 'turn-dot waiting';
    text.textContent = S.game.is_vs_cpu
      ? 'Starting…'
      : '⏳ Waiting for an opponent to join…';
    return;
  }
  if (S.game.status === 'completed') {
    dot.className = 'turn-dot';
    text.textContent = '✅ Game finished!';
    return;
  }

  if (S.game.status === 'active' && !S.myColor) {
    dot.className = 'turn-dot';
    const { turn } = parseFEN(S.game.fen);
    text.textContent = turn === 'w' ? '👀 White to move' : '👀 Black to move';
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
    const thinking = S.game.is_vs_cpu && S.myColor === 'white'
      ? 'Computer is thinking…'
      : 'Opponent is thinking…';
    text.textContent = '⏳ ' + thinking;
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

  const lossQuotes = [
    '“Every chess master was once a beginner.” Keep practising — your next game is another step forward.',
    '“The blunders are all there on the board, waiting to be made.” — Savielly Tartakower. Study the game, and you’ll spot more of them next time.',
    'A loss is just feedback. Strong players treat every game as a lesson in disguise.',
  ];
  const randomQuote = lossQuotes[Math.floor(Math.random() * lossQuotes.length)];

  let isWin = false;
  if (result === 'white' && S.myColor === 'white') isWin = true;
  if (result === 'black' && S.myColor === 'black') isWin = true;
  const isDraw = result === 'draw';

  const reasonNote = (() => {
    if (reason === 'time_forfeit') return ' (on time)';
    if (reason === 'resignation') return ' (resignation)';
    return '';
  })();

  if (S.myColor == null) {
    icon.textContent = '👀';
    title.textContent = isDraw ? "It's a Draw!" : 'Game over';
    msg.textContent = isDraw
      ? 'This game ended in a draw.'
      : (result === 'white' ? 'White won this game.' : 'Black won this game.');
    prize.textContent = '';
    overlay.classList.add('open');
    if (S.video) { S.video.stop(); S.video = null; }
    return;
  }

  if (isDraw) {
    icon.textContent  = '🤝';
    title.textContent = "It's a Draw!";
    msg.textContent   = 'Great game! Your bet has been refunded.';
    prize.textContent = `₹${S.game.bet_amount.toFixed(2)} refunded`;
  } else if (isWin) {
    icon.textContent  = '🏆';
    title.textContent = 'You Won!!! 🎉';
    const pvpExtra = (S.game && !S.game.is_vs_cpu)
      ? ' Head-to-head games require usable video from both you and your opponent for verification.'
      : '';
    msg.textContent =
      `Great game — you earned this${reasonNote}. ` +
      'Within 24 hours, your winnings can be credited to your wallet after our review confirms a legitimate win (fair play and any required video checks). ' +
      'If something doesn’t meet the rules, the payout may be held or not approved.' + pvpExtra;
    prize.textContent = `Up to ₹${(S.game.bet_amount * 2).toFixed(2)} (after platform fee) · pending payout`;
    spawnConfetti();
  } else {
    icon.textContent  = '♟';
    title.textContent = 'Better luck next time!';
    const lossLead = reason === 'time_forfeit'
      ? 'The clock ran out this time — manage your time and try again. '
      : `Tough game${reasonNote}. `;
    msg.textContent = lossLead + randomQuote;
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
    // Timeslice so the browser emits a blob every N ms — otherwise you only get
    // one short clip at stop() and admins cannot review a 5+ minute game.
    const SLICE_MS = 15000;
    this.recorder.ondataavailable = (e) => {
      if (!e.data || e.data.size < 256) return;
      const idx = this.chunkNum++;
      API.uploadChunk(this.gameId, e.data, idx)
        .catch(err => console.warn('Chunk upload failed:', err));
    };
    try {
      this.recorder.start(SLICE_MS);
    } catch (_e) {
      try { this.recorder.start(); } catch (_e2) { /* last resort */ }
    }
  }

  stop() {
    this.active = false;
    try {
      if (this.recorder && this.recorder.state === 'recording') {
        try { this.recorder.requestData(); } catch (_e) { /* ignore */ }
      }
      if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop();
      if (this.stream) this.stream.getTracks().forEach(t => t.stop());
    } catch(e) { /* ignore */ }
    document.getElementById('cam-preview').srcObject = null;
    document.getElementById('cam-status').textContent = 'Recording stopped';
  }
}

async function promptWebcam() {
  const vsCpu = S.game?.is_vs_cpu;
  const body = vsCpu
    ? `Recording helps approve your win. Video is uploaded in ~15 second segments during the game (so reviewers see the full session, not just the last few seconds). For vs-CPU, only your side needs usable video on file. (₹${S.game.bet_amount} table)`
    : `Multiplayer: reviewers need usable video from both players. The camera uploads ~15 second segments throughout the game so the recording length matches real play time. If either side has no usable recording, payouts may be denied. (₹${S.game.bet_amount} table)`;
  const confirmed = await Modal.show(
    '📹',
    'Webcam & prize review',
    body,
    'Enable Camera 📷',
    'Continue without camera',
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

function lobbyCardActions(g) {
  const uid = S.user?.id;
  const imWhite = uid === g.white_player_id;
  const imBlack = g.black_player_id != null && uid === g.black_player_id;
  const imPlayer = imWhite || imBlack;

  if (g.status === 'waiting') {
    if (imWhite && !g.is_vs_cpu) {
      return `
        <button type="button" class="btn btn-primary btn-block" onclick="resumeGame(${g.id})">Open table ♟</button>
        <button type="button" class="btn btn-ghost btn-block" style="margin-top:6px" onclick="cancelWaitingGame(${g.id})">Cancel lobby 🗑️</button>`;
    }
    if (!imWhite && !g.is_vs_cpu) {
      return `<button type="button" class="btn btn-primary btn-block" onclick="joinGame(${g.id})">Join Game ♟</button>`;
    }
    return '<span style="font-size:13px;color:#888">—</span>';
  }
  if (g.status === 'active') {
    if (imPlayer) {
      return `<button type="button" class="btn btn-primary btn-block" onclick="resumeGame(${g.id})">Resume game ♟</button>`;
    }
    return `<button type="button" class="btn btn-secondary btn-block" onclick="watchGame(${g.id})">Watch 👀</button>`;
  }
  return `<button type="button" class="btn btn-ghost btn-block" onclick="viewGame(${g.id})">View ♟</button>`;
}

async function resumeGame(gameId) {
  try {
    const game = await API.getGame(gameId);
    if (!S.user) {
      Toast.err('Please log in again.');
      return;
    }
    const myColor = S.user.id === game.white_player_id ? 'white' : 'black';
    if (S.user.id !== game.white_player_id && S.user.id !== game.black_player_id) {
      Toast.warn('You are not a player in this game — opening as spectator.');
      await watchGame(gameId);
      return;
    }
    enterGame(game, myColor);
  } catch (e) {
    Toast.err(e.message || 'Could not open game');
  }
}

async function cancelWaitingGame(gameId) {
  const ok = await Modal.show(
    '🗑️',
    'Cancel this table?',
    'Your locked bet will be released and you can start a new game.',
    'Yes, cancel',
    'Keep waiting',
  );
  if (!ok) return;
  try {
    await API.cancelGame(gameId);
    Toast.ok('Lobby cancelled. Your stake is unlocked.');
    await loadLobby();
    if (S.game && S.game.id === gameId) {
      App.goLobby();
    }
  } catch (e) {
    Toast.err(e.message || 'Could not cancel');
  }
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
          <span style="font-size:13px;color:#888">#${g.id}${g.is_vs_cpu ? ' · 🤖 CPU' : ''}</span>
        </div>
        <div class="gc-player">♟ ${g.white_username}</div>
        <div class="gc-bet-label">Prize Pool</div>
        <div class="gc-bet">₹${(g.bet_amount * 2).toFixed(2)}</div>
        <div class="gc-action">
          ${lobbyCardActions(g)}
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
    <p style="margin-bottom:12px;color:#7578A8">You always play <strong>White</strong>. Prize pool is 2× your stake. Available: <strong>₹${available.toFixed(2)}</strong></p>
    <div style="margin-bottom:14px">
      <div style="font-size:12px;font-weight:800;color:#5349D1;margin-bottom:6px">OPPONENT</div>
      <label style="display:flex;gap:8px;align-items:center;margin-bottom:6px;cursor:pointer">
        <input type="radio" name="create-opp" value="human" checked />
        <span>Wait for a friend (share invite)</span>
      </label>
      <label style="display:flex;gap:8px;align-items:center;cursor:pointer">
        <input type="radio" name="create-opp" value="cpu" />
        <span>Play computer now (no waiting)</span>
      </label>
    </div>
    <div style="margin-bottom:12px">
      <label class="field-label" style="display:block;margin-bottom:6px">⏱ Clock</label>
      <select class="field-input" id="clock-preset" style="width:100%">
        <option value="180|2">Blitz 3 min + 2 sec / move</option>
        <option value="300|3">Blitz 5 min + 3 sec / move</option>
        <option value="600|5" selected>Rapid 10 min + 5 sec / move</option>
        <option value="900|10">Rapid 15 min + 10 sec / move</option>
        <option value="1800|0">30 min + 0 (no increment)</option>
      </select>
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-bottom:12px">
      ${amounts.map(a => `<button type="button" class="quick-chip" onclick="document.getElementById('bet-input').value=${a};document.querySelectorAll('#modal-body .quick-chip').forEach(c=>c.classList.remove('active'));this.classList.add('active')" style="min-width:70px">₹${a}</button>`).join('')}
    </div>
    <input class="field-input" type="number" id="bet-input" placeholder="Bet ₹1–₹${Math.min(100,available).toFixed(0)}" min="1" max="${Math.min(100,available)}" step="1" style="margin-bottom:10px" />
    <label style="display:flex;gap:10px;align-items:flex-start;cursor:pointer;font-size:13px;line-height:1.35;color:#333">
      <input type="checkbox" id="prize-terms-ack" style="margin-top:3px" />
      <span>I understand: for <strong>head-to-head games</strong>, <strong>both players</strong> need usable webcam footage on file before a win can be verified and paid; for <strong>vs computer</strong>, only your recording matters. If video is off, unclear, or missing, prize money may not be credited.</span>
    </label>
    <small style="color:#7578A8;display:block;text-align:center;margin-top:8px">Max ₹100 per bet · <a href="#" onclick="return false" style="pointer-events:none">Terms required to create a cash table</a></small>`;
  document.getElementById('modal-body').classList.add('modal-body--form');
  document.getElementById('modal-cancel').textContent = 'Cancel';
  document.getElementById('modal-ok').textContent     = 'Create Game! ♟';
  overlayEl.classList.add('open');

  Modal._cb = async (ok) => {
    Modal._cb = null;
    overlayEl.classList.remove('open');
    if (!ok) return;
    const amt = parseFloat(document.getElementById('bet-input').value);
    if (!amt || amt < 1) { Toast.err('Enter a valid bet amount!'); return; }
    const ack = document.getElementById('prize-terms-ack')?.checked;
    if (!ack) {
      Toast.err('Please confirm the prize & webcam eligibility notice to play for money.');
      return;
    }
    const opp = document.querySelector('input[name="create-opp"]:checked')?.value || 'human';
    const preset = (document.getElementById('clock-preset')?.value || '600|5').split('|');
    const clock_initial_sec = parseInt(preset[0], 10) || 600;
    const clock_increment_sec = parseInt(preset[1], 10) || 0;
    try {
      const game = await API.createGame({
        bet_amount: amt,
        vs_cpu: opp === 'cpu',
        video_prize_terms_ack: true,
        clock_initial_sec,
        clock_increment_sec,
      });
      Toast.ok(
        opp === 'cpu'
          ? `Game #${game.id} — White vs computer!`
          : `Game #${game.id} — share the invite with a friend.`,
      );
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
    document.getElementById('opp-name').textContent = game.is_vs_cpu ? 'Computer' : 'Player';
    document.getElementById('my-name').textContent = 'Spectator';
    if (game.status === 'active') {
      syncClockFromGame();
      startClockTicker();
      connectWS(game.id);
    } else {
      stopClockTicker();
    }
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
    if (game.is_vs_cpu && myColor === 'white') {
      document.getElementById('opp-name').textContent = 'Computer';
    } else if (oppId) {
      try {
        const opp = await API.getUser(oppId);
        document.getElementById('opp-name').textContent = opp.username;
      } catch(e) { /* ignore */ }
    }
  } else {
    document.getElementById('opp-name').textContent = 'Waiting for opponent…';
  }

  if (game.status === 'active') {
    syncClockFromGame();
    startClockTicker();
  } else {
    stopClockTicker();
    const wEl = document.getElementById('clock-white-val');
    const bEl = document.getElementById('clock-black-val');
    if (wEl) wEl.textContent = '—';
    if (bEl) bEl.textContent = '—';
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
    const [wallet, txs, payouts, deposits, withdrawals] = await Promise.all([
      API.getWallet(),
      API.transactions(),
      API.myPayouts(),
      API.myDepositReqs(),
      API.myWithdrawals(),
    ]);
    S.wallet = wallet;

    document.getElementById('w-balance').textContent = `₹${wallet.balance.toFixed(2)}`;
    document.getElementById('w-locked').textContent  = `₹${wallet.total_invested.toFixed(2)}`;
    const withdrawable = Math.max(0, Math.round((wallet.balance - wallet.total_invested) * 100) / 100);
    const wW = document.getElementById('w-withdrawable');
    if (wW) wW.textContent = `₹${withdrawable.toFixed(2)}`;
    const wMaxHint = document.getElementById('withdraw-max-hint');
    if (wMaxHint) {
      wMaxHint.textContent = withdrawable > 0
        ? `You may withdraw up to ₹${withdrawable.toFixed(2)} right now.`
        : 'Nothing to withdraw yet (funds may be locked in an active game).';
    }
    const pct = Math.min(100, (wallet.balance / 100) * 100);
    document.getElementById('w-progress').style.width = `${pct}%`;
    document.getElementById('nav-balance').textContent = `₹${wallet.balance.toFixed(2)}`;

    // Payout history (pending / approved / rejected)
    const pList = document.getElementById('payouts-list');
    if (!payouts.length) {
      pList.innerHTML = '<div style="font-size:13px;color:#7578A8">No payout records yet.</div>';
    } else {
      pList.innerHTML = payouts.map(p => {
        if (p.status === 'pending') {
          const release = p.auto_release_at ? new Date(p.auto_release_at) : null;
          const hoursLeft = release ? Math.max(0, ((release - Date.now()) / 3600000)).toFixed(1) : '?';
          return `<div class="tx-item" style="margin-bottom:8px">
            <div class="tx-icon">⏳</div>
            <div class="tx-desc">
              <div class="tx-desc-title">Game #${p.game_id} · awaiting release</div>
              <div class="tx-desc-date">Auto / review ~${hoursLeft}h left · Fee ₹${Number(p.platform_fee).toFixed(2)}</div>
            </div>
            <div class="tx-amount pos">₹${Number(p.net_amount).toFixed(2)}</div>
          </div>`;
        }
        if (p.status === 'approved') {
          return `<div class="tx-item" style="margin-bottom:8px">
            <div class="tx-icon">✅</div>
            <div class="tx-desc">
              <div class="tx-desc-title">Game #${p.game_id} · credited to wallet</div>
              <div class="tx-desc-date">Net ₹${Number(p.net_amount).toFixed(2)} added to balance — use Withdraw below to cash out (~24h to your UPI/bank)</div>
            </div>
            <div class="tx-amount pos">+₹${Number(p.net_amount).toFixed(2)}</div>
          </div>`;
        }
        if (p.status === 'rejected' || p.status === 'penalized') {
          return `<div class="tx-item" style="margin-bottom:8px">
            <div class="tx-icon">❌</div>
            <div class="tx-desc">
              <div class="tx-desc-title">Game #${p.game_id} · payout not released</div>
              <div class="tx-desc-date">${(p.rejection_reason || 'Rejected').slice(0, 120)}</div>
            </div>
            <div class="tx-amount neg">—</div>
          </div>`;
        }
        return `<div class="tx-item" style="margin-bottom:8px"><div class="tx-desc">${p.status}</div></div>`;
      }).join('');
    }

    // My withdrawal requests (UPI)
    const wDrawList = document.getElementById('my-withdrawals-list');
    if (wDrawList) {
      if (!withdrawals.length) {
        wDrawList.innerHTML = '<div class="empty-state">No withdrawal requests yet</div>';
      } else {
        const wIcons = { pending: '⏳', completed: '✅', rejected: '❌' };
        const escMini = (s) =>
          String(s || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
        wDrawList.innerHTML = withdrawals.map(w => `
        <div class="tx-item">
          <div class="tx-icon">${wIcons[w.status] || '📤'}</div>
          <div class="tx-desc">
            <div class="tx-desc-title">₹${Number(w.amount).toFixed(2)} → ${escMini(w.destination_upi)}</div>
            <div class="tx-desc-date">${new Date(w.created_at).toLocaleString('en-IN')}${w.rejection_reason ? ' · ' + escMini(w.rejection_reason).slice(0, 200) : ''}</div>
          </div>
          <div class="tx-amount" style="font-size:13px;font-weight:800;color:${w.status === 'completed' ? '#43C59E' : w.status === 'rejected' ? '#FF5252' : '#FF9800'}">${String(w.status).toUpperCase()}</div>
        </div>`).join('');
      }
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
              <div class="tx-desc-date">${new Date(tx.created_at).toLocaleString('en-IN')}${tx.type === 'withdrawal' ? ' · Bank/UPI credit usually within ~24h' : ''}</div>
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
  connectUserWS();
  showView('lobby');
  loadLobby();
}

function logout() {
  S.token = S.user = S.game = S.wallet = null;
  stopClockTicker();
  closeWS({ manual: true });
  closeUserWS();
  if (S.video) { S.video.stop(); S.video = null; }
  localStorage.removeItem('cw_token');
  document.getElementById('navbar').classList.remove('visible');
  showView('landing');
}

// ════════════════════════════════════════════════════════════════════════════
//  Per-user notification WebSocket (account-wide events)
// ════════════════════════════════════════════════════════════════════════════
function closeUserWS() {
  if (S.userWsReconnectTimer) {
    clearTimeout(S.userWsReconnectTimer);
    S.userWsReconnectTimer = null;
  }
  if (S.userWs) {
    try { S.userWs.onclose = null; S.userWs.close(); } catch (_e) { /* ignore */ }
    S.userWs = null;
  }
}

function connectUserWS() {
  closeUserWS();
  if (!S.token) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/users/me/ws?token=${encodeURIComponent(S.token)}`;
  let ws;
  try {
    ws = new WebSocket(url);
  } catch (_e) {
    return;
  }
  S.userWs = ws;
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_e) { return; }
    handleUserWSMessage(msg);
  };
  ws.onclose = () => {
    if (!S.token) return;          // user logged out — don't reconnect
    if (S.userWsReconnectTimer) return;
    S.userWsReconnectTimer = setTimeout(() => {
      S.userWsReconnectTimer = null;
      connectUserWS();
    }, 4000);
  };
  ws.onerror = () => { /* let onclose handle reconnect */ };
}

function handleUserWSMessage(msg) {
  if (!msg || !msg.type) return;
  const data = msg.data || {};
  // Update nav balance immediately on any balance-bearing event.
  if (typeof data.balance === 'number') {
    const navEl = document.getElementById('nav-balance');
    if (navEl) navEl.textContent = `₹${Number(data.balance).toFixed(2)}`;
    if (S.wallet) S.wallet.balance = data.balance;
    const wBal = document.getElementById('w-balance');
    if (wBal) wBal.textContent = `₹${Number(data.balance).toFixed(2)}`;
    const lsBal = document.getElementById('ls-balance');
    if (lsBal) lsBal.textContent = `₹${Number(data.balance).toFixed(2)}`;
  }
  switch (msg.type) {
    case 'ready':
    case 'ping':
    case 'pong':
      return;
    case 'wallet_update':
      Toast.ok(data.reason || 'Your wallet was updated.');
      break;
    case 'deposit_approved':
      Toast.ok(data.message || `Deposit of ₹${data.amount} approved! 🎉`);
      break;
    case 'deposit_rejected':
      Toast.warn(data.message || 'Deposit request was declined.');
      break;
    case 'withdrawal_completed':
      Toast.ok(data.message || 'Withdrawal paid out.');
      break;
    case 'withdrawal_rejected':
      Toast.warn(data.message || 'Withdrawal cancelled and refunded.');
      break;
    case 'payout_approved':
      Toast.ok(data.message || 'Winnings released to your wallet!');
      break;
    case 'payout_rejected':
      Toast.err(data.message || 'Payout was declined.');
      break;
    case 'account_banned':
      Toast.err(data.message || 'Your account has been suspended.');
      break;
    case 'account_unbanned':
      Toast.ok(data.message || 'Your account has been reactivated.');
      break;
  }
  // If the wallet view is currently active, do a full refresh so any
  // related lists (deposits, withdrawals, payouts) reflect the change too.
  const walletView = document.getElementById('view-wallet');
  if (walletView && walletView.classList.contains('active') && typeof loadWallet === 'function') {
    loadWallet();
  }
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
    stopClockTicker();
    closeWS({ manual: true });
    if (S.video) { S.video.stop(); S.video = null; }
    S.game = null;
    showView('lobby');
    loadLobby();
  },
};

async function leaveToLobby() {
  if (S.game && S.game.status === 'active' && S.myColor) {
    const ok = await Modal.show(
      '🏠',
      'Leave the table?',
      'You can rejoin from Open Games or Live Now. To concede the result, use Resign.',
      'Leave to lobby',
      'Stay',
    );
    if (!ok) return;
  }
  App.goLobby();
}
window.App = App;
window.leaveToLobby = leaveToLobby;
window.showView = showView;
window.joinGame = joinGame;
window.watchGame = watchGame;
window.viewGame = viewGame;
window.resumeGame = resumeGame;
window.cancelWaitingGame = cancelWaitingGame;

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
      afterLogin(data.access_token, data.user);
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
      Toast.ok(`Welcome back, ${data.user.username}! ♟`);
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

  document.getElementById('back-to-lobby-btn')?.addEventListener('click', () => leaveToLobby());

  document.querySelectorAll('.page-back-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (btn.dataset.backView === 'lobby') {
        showView('lobby');
        loadLobby();
      }
    });
  });

  // ── Modal buttons ──
  document.getElementById('modal-ok').addEventListener('click', () => Modal.close(true));
  document.getElementById('modal-cancel').addEventListener('click', () => Modal.close(false));

  // ── Webcam start button ──
  document.getElementById('start-cam-btn').addEventListener('click', async () => {
    if (!S.video) S.video = new VideoMonitor(S.game?.id || 0);
    await S.video.start();
  });

  // ── Resign ──
  // Design notes:
  //  • The user just wants out — never get stuck on "Resigning…".
  //  • We show the result UI IMMEDIATELY on confirm so the player can leave.
  //  • The actual POST /resign runs in the background with a hard timeout.
  //  • If the request times out or fails, we toast a warning but still let
  //    the player return to the lobby — server will reconcile on next visit
  //    (or admin can settle).
  document.getElementById('resign-btn').addEventListener('click', async () => {
    if (!S.game || S.game.status !== 'active') return;

    const bet = Number(S.game.bet_amount) || 0;
    const feeNote = bet > 0
      ? ` Your locked stake (₹${bet.toFixed(2)}) is treated as forfeited on resignation — you will not get it back, and you may lose eligibility for winnings or participation fees for this table.`
      : ' Your opponent will be awarded the result.';
    const ok = await Modal.show('🏳️', 'Resign this game?',
      `Are you sure? Your opponent wins the match.${feeNote}`,
      'Yes, I resign', 'Keep playing');
    if (!ok) return;

    clearMoveLock();
    closeWS({ manual: true });
    const gameId = S.game.id;
    const winner = S.myColor === 'white' ? 'black' : 'white';

    // 1) Reflect resignation in the UI right now. The player is done.
    if (S.game) S.game.status = 'completed';
    stopClockTicker();
    showResult(winner, 'resignation');

    // 2) Fire the REST resign in the background; never let it block the UI.
    (async () => {
      try {
        await API.resign(gameId);
      } catch (e) {
        const msg = String(e?.message || '').toLowerCase();
        if (msg.includes('not active') || msg.includes('completed') || msg.includes('finished')) {
          return; // already ended on the server — fine
        }
        // Surface the problem but DO NOT undo the UI; the player has moved on.
        Toast.warn(
          'Resignation could not be confirmed with the server right now — ' +
          'it will be retried automatically. You can safely leave.'
        );
        // Retry once after a short backoff in case the server was restarting.
        setTimeout(() => {
          API.resign(gameId).catch(() => {});
        }, 3000);
      }
    })();
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
    const upiEl = document.getElementById('withdraw-upi');
    const upi = (upiEl && upiEl.value) ? upiEl.value.trim() : '';
    if (!amt || amt < 1) { Toast.err('Enter a valid amount'); return; }
    if (!upi || !upi.includes('@')) {
      Toast.err('Enter your Google Pay / UPI ID (e.g. name@paytm or name@oksbi)');
      return;
    }
    try {
      await API.withdraw(amt, upi);
      Toast.show(
        `Withdrawal of ₹${amt.toFixed(2)} to <strong>${upi}</strong> submitted. UPI payout is usually within about <strong>24 hours</strong> (processed manually).`,
        'success',
        9000,
      );
      document.getElementById('withdraw-amount').value = '';
      if (upiEl) upiEl.value = '';
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
