'use strict';

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const codeFromUrl = (params.get('code') || '').trim().toUpperCase();
const page = location.pathname;
const letters = ['A', 'B', 'C', 'D'];
let roomCode = codeFromUrl;
let sessionToken = '';
let state = null;
let events = null;
let toastTimer = null;
let draftTimer = null;
let nextRevision = 0;
let teacherIsLoaded = false;
let lastTeacherPhase = '';
let answerBusy = false;
let actionBusy = false;

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
}
function number(value) { return Number(value || 0).toLocaleString('ko-KR'); }
function multiplierLabel(value) { return `${Number(value || 1).toLocaleString('ko-KR', {maximumFractionDigits:2})}배`; }
function showToast(message, isError = false) {
  const el = $('toast');
  el.textContent = message;
  el.classList.toggle('error', isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4200);
}
function setConnected(online) {
  $('connectionDot').classList.toggle('offline', !online);
  $('connectionText').textContent = online ? '실시간 연결됨' : '연결 재시도 중…';
}
async function api(endpoint, data = {}, token = '') {
  let response;
  try {
    response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(token ? {'X-Session-Token':token} : {}) },
      body: JSON.stringify(data),
    });
  } catch (e) { throw new Error('서버에 연결할 수 없습니다. 주소와 와이파이를 확인해 주세요.'); }
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || '요청을 처리하지 못했습니다.');
  return result;
}
async function getState(code, token) {
  const response = await fetch(`/api/rooms/${encodeURIComponent(code)}/state?token=${encodeURIComponent(token)}`, {cache:'no-store'});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || '입장 정보를 확인하지 못했습니다.');
  return result;
}
function connectToRoom() {
  if (events) events.close();
  events = new EventSource(`/api/rooms/${encodeURIComponent(roomCode)}/events?token=${encodeURIComponent(sessionToken)}`);
  events.onopen = () => setConnected(true);
  events.onerror = () => setConnected(false);
  events.onmessage = (event) => {
    try {
      const latest = JSON.parse(event.data);
      if (state && latest.version < state.version) return;
      state = latest;
      if (page === '/teacher') renderTeacher();
      else renderStudent();
    } catch (e) { console.warn('Invalid update', e); }
  };
}
function showView(id) { $(id).hidden = false; }
async function initHome() {
  showView('homeView');
  if (codeFromUrl) $('roomCodeInput').value = codeFromUrl;
  $('roomCodeInput').addEventListener('input', (event) => {
    event.target.value = event.target.value.replace(/[^A-Za-z0-9]/g, '').toUpperCase();
  });
  $('createRoomBtn').addEventListener('click', async () => {
    const btn = $('createRoomBtn'); btn.disabled = true; btn.textContent = '방을 만드는 중…';
    try {
      const result = await api('/api/rooms', {});
      localStorage.setItem(`toktok_teacher_${result.code}`, result.token);
      location.href = `/teacher?code=${encodeURIComponent(result.code)}`;
    } catch (error) {
      showToast(error.message, true);
      btn.disabled = false; btn.textContent = '새 퀴즈방 만들기 ↗';
    }
  });
  $('joinForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const code = $('roomCodeInput').value.trim().toUpperCase();
    const name = $('studentNameInput').value.trim();
    if (code.length !== 6) return showToast('6자리 참여 코드를 입력해 주세요.', true);
    if (!name) return showToast('이름 또는 별명을 입력해 주세요.', true);
    const btn = $('joinForm').querySelector('button'); btn.disabled = true;
    try {
      // Preserve identity on a refresh, avoiding accidental duplicate names.
      const existing = localStorage.getItem(`toktok_student_${code}`);
      if (existing) {
        try {
          const old = await getState(code, existing);
          if (old.you && old.you.name === name) {
            location.href = `/play?code=${encodeURIComponent(code)}`;
            return;
          }
        } catch (ignored) { /* expired or invalid identity */ }
      }
      const result = await api(`/api/rooms/${encodeURIComponent(code)}/join`, {name});
      localStorage.setItem(`toktok_student_${code}`, result.token);
      location.href = `/play?code=${encodeURIComponent(code)}`;
    } catch (error) { showToast(error.message, true); btn.disabled = false; }
  });
}
function draftPayload() {
  const correct = document.querySelector('input[name="correct"]:checked');
  return {
    question: $('questionInput').value,
    options: [...document.querySelectorAll('[data-opt]')].map((el) => el.value),
    correct_index: correct ? Number(correct.value) : null,
    multiplier: $('multiplierInput').value.trim(),
  };
}
function updatePreview(source = null) {
  const item = source || (state && state.status !== 'draft' ? state : draftPayload());
  if (!item) return;
  const options = item.options || ['', '', '', ''];
  const label = item.status === 'live' ? '🟢 문제 풀이 중' : item.status === 'results' ? '🏆 결과 확인 중' : '🟡 준비 중 · 학생도 보게 됩니다';
  $('teacherPreview').innerHTML = `
    <div class="preview-label">${label} <span class="preview-multiplier">✨ ${multiplierLabel(item.multiplier)}</span></div>
    <div class="preview-question">${esc(item.question?.trim() || '아직 작성된 문제가 없어요 ✏️')}</div>
    ${options.map((choice, index) => choice.trim() ? `<div class="preview-choice"><b>${letters[index]}</b> ${esc(choice)}</div>` : '').join('')}
    <div class="preview-bottom">학생 화면에는 정답 체크 표시가 나타나지 않습니다.</div>
  `;
}
function updateMultiplierPresets() {
  const value = $('multiplierInput').value.trim();
  document.querySelectorAll('[data-multiplier]').forEach(button => {
    const selected = value !== '' && Number(value) === Number(button.dataset.multiplier);
    button.classList.toggle('selected', selected);
    button.setAttribute('aria-pressed', String(selected));
  });
}
function scheduleDraft() {
  if (!state || state.status !== 'draft') return;
  updateMultiplierPresets();
  updatePreview();
  clearTimeout(draftTimer);
  draftTimer = setTimeout(async () => {
    const revision = ++nextRevision;
    const body = {...draftPayload(), revision};
    try { await api(`/api/rooms/${roomCode}/draft`, body, sessionToken); }
    catch (error) { if (state?.status === 'draft') showToast(error.message, true); }
  }, 110);
}
function syncEditor(snapshot) {
  $('questionInput').value = snapshot.question || '';
  const opts = document.querySelectorAll('[data-opt]');
  opts.forEach((el, i) => {el.value = snapshot.options[i] || '';});
  document.querySelectorAll('input[name="correct"]').forEach((el) => {
    el.checked = snapshot.correct_index === Number(el.value);
  });
  $('multiplierInput').value = String(snapshot.multiplier ?? 1);
  updateMultiplierPresets();
  nextRevision = Math.max(nextRevision, snapshot.draft_revision + 1);
  updatePreview(snapshot);
}
function setEditorDisabled(disabled) {
  $('questionInput').disabled = disabled;
  document.querySelectorAll('[data-opt],input[name="correct"],[data-multiplier]').forEach(el => {el.disabled = disabled;});
  $('multiplierInput').disabled = disabled;
}
function renderTeacher() {
  if (!state) return;
  const phase = `${state.round_no}:${state.status}`;
  if (!teacherIsLoaded || (state.status === 'draft' && phase !== lastTeacherPhase)) {
    syncEditor(state);
    teacherIsLoaded = true;
  }
  lastTeacherPhase = phase;
  const live = state.status === 'live', results = state.status === 'results';
  $('teacherCode').textContent = state.code;
  $('teacherRound').textContent = state.round_no;
  $('teacherCount').textContent = state.participants;
  $('teacherSubmitted').textContent = state.submitted;
  $('teacherStatus').textContent = live ? '🟢 퀴즈 진행 중' : results ? '🏆 정답 · 순위 공개' : '🟡 문제 준비 중';
  $('teacherStatus').className = 'state-chip' + (live ? ' live' : results ? ' results' : '');
  $('startBtn').hidden = live || results;
  $('closeBtn').hidden = !live;
  $('nextBtn').hidden = !results;
  $('startBtn').disabled = actionBusy;
  $('closeBtn').disabled = actionBusy;
  $('nextBtn').disabled = actionBusy;
  setEditorDisabled(live || results);
  $('teacherActionHint').textContent = live ? '학생들이 답안을 제출하고 있어요. 종료하면 정답과 순위가 공개됩니다.' : results ? '점수는 다음 문제에 누적됩니다. 이어서 다음 문제를 만들어 주세요!' : '질문과 보기를 타이핑하면 학생들이 실시간으로 볼 수 있어요.';
  if (live || results) updatePreview(state);
  else updatePreview();
  renderRoster();
}
function renderRoster() {
  if (!state || !state.players) return;
  const players = [...state.players];
  if (state.status === 'results') players.sort((a, b) => b.score - a.score);
  $('rosterTitle').textContent = state.status === 'results' ? '최종 순위' : '참여 학생';
  $('rosterCount').textContent = `${players.length}명`;
  if (!players.length) {
    $('teacherRoster').innerHTML = '<p class="empty-hint">학생들이 입장하면 여기에 나타나요.</p>';
    return;
  }
  $('teacherRoster').innerHTML = players.map((p, idx) => {
    let meta = state.status === 'results' ? `${number(p.score)}점` : state.status === 'live' ? (p.submitted ? '✓ 제출 완료' : '답변 대기 중') : `${number(p.score)}점`;
    if (state.status === 'results') meta = `${idx + 1}위 · ${meta}`;
    return `<div class="roster-row"><div class="roster-person"><span class="avatar">${esc([...p.name][0] || '?')}</span><span class="roster-name">${esc(p.name)}</span></div><span class="roster-meta ${p.submitted ? 'answered' : ''}">${esc(meta)}</span></div>`;
  }).join('');
}
async function teacherAction(action, payload = {}) {
  if (actionBusy) return;
  actionBusy = true;
  ['startBtn','closeBtn','nextBtn'].forEach(id => {$(id).disabled = true;});
  try { await api(`/api/rooms/${roomCode}/${action}`, payload, sessionToken); }
  catch (error) { showToast(error.message, true); }
  finally { actionBusy = false; ['startBtn','closeBtn','nextBtn'].forEach(id => {$(id).disabled = false;}); }
}
async function copyText(text, successMessage) {
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
    else {
      const temp = document.createElement('textarea'); temp.value = text;
      document.body.appendChild(temp); temp.select();
      if (!document.execCommand('copy')) throw new Error('clipboard');
      temp.remove();
    }
    showToast(successMessage);
  } catch (error) { showToast('복사 기능을 사용할 수 없습니다. 화면의 코드를 직접 알려 주세요.', true); }
}
async function initTeacher() {
  showView('teacherView');
  if (!roomCode) {location.replace('/');return;}
  sessionToken = localStorage.getItem(`toktok_teacher_${roomCode}`) || '';
  if (!sessionToken) {showToast('선생님 입장 정보가 없습니다. 새 방을 만들어 주세요.', true);setTimeout(() => location.replace('/'), 1800);return;}
  try {
    state = await getState(roomCode, sessionToken);
    if (state.role !== 'teacher') throw new Error('교사 인증을 확인하지 못했습니다.');
  } catch (e) {showToast(e.message, true);setTimeout(() => location.replace('/'), 2400);return;}
  $('teacherCode').textContent = roomCode;
  $('copyCodeBtn').addEventListener('click', () => copyText(roomCode, '참여 코드가 복사되었습니다!'));
  $('copyLinkBtn').addEventListener('click', () => copyText(`${location.origin}/join?code=${encodeURIComponent(roomCode)}`, '학생 입장 링크가 복사되었습니다!'));
  $('questionInput').addEventListener('input', scheduleDraft);
  document.querySelectorAll('[data-opt]').forEach(el => el.addEventListener('input', scheduleDraft));
  document.querySelectorAll('input[name="correct"]').forEach(el => el.addEventListener('change', scheduleDraft));
  $('multiplierInput').addEventListener('input', scheduleDraft);
  document.querySelectorAll('[data-multiplier]').forEach(el => el.addEventListener('click', () => {
    $('multiplierInput').value = el.dataset.multiplier;
    scheduleDraft();
  }));
  $('startBtn').addEventListener('click', () => {
    clearTimeout(draftTimer);
    teacherAction('start', draftPayload());
  });
  $('closeBtn').addEventListener('click', () => {
    if (window.confirm('문제를 종료하고 학생들에게 정답과 순위를 공개할까요?')) teacherAction('close');
  });
  $('nextBtn').addEventListener('click', () => teacherAction('next'));
  renderTeacher();
  connectToRoom();
}
function renderResults() {
  const el = $('studentResults');
  if (state.status !== 'results') { el.hidden = true;return; }
  const board = state.leaderboard || [];
  const me = state.you.name;
  const rank = board.find(p => p.name === me);
  const earned = state.you.earned || 0;
  const phrase = state.you.was_correct === true ? '🎉 정답이에요! 멋져요!' : state.you.was_correct === false ? '조금 아쉬워요! 다음 문제에서 도전해요.' : '이번 문제에서는 답을 제출하지 않았어요.';
  el.hidden = false;
  el.innerHTML = `<h2>🏆 이번 문제 결과</h2><div class="result-summary">${esc(phrase)} · 이번 문제 <b>+${number(earned)}점</b> · 내 순위 <b>${rank ? rank.rank : '-'}위</b></div>
    <div class="results-list">${board.map(p => `<div class="result-row ${p.name === me ? 'me' : ''}"><span class="result-rank">${p.rank === 1 ? '🥇' : p.rank === 2 ? '🥈' : p.rank === 3 ? '🥉' : `${p.rank}위`}</span><span class="result-name">${esc(p.name)} ${p.name === me ? '(나)' : ''}</span><span class="result-score">${number(p.score)}점</span></div>`).join('')}</div>`;
}
function choiceButton(choice, index) {
  const live = state.status === 'live';
  const results = state.status === 'results';
  const already = state.you.answer !== null;
  const isSelected = state.you.answer === index;
  const isCorrect = results && state.correct_index === index;
  let classes = ['answer-button'];
  if (isCorrect) classes.push('correct');
  else if (results && isSelected) classes.push('incorrect');
  else if (isSelected) classes.push('selected');
  if (!live && !results) classes.push('disabled-preview');
  if (already && !isSelected && !results) classes.push('muted');
  const button = document.createElement('button');
  button.type = 'button';
  button.className = classes.join(' ');
  button.disabled = !live || already || answerBusy;
  const letter = document.createElement('span'); letter.textContent = letters[index];
  const text = document.createElement('div');text.textContent = choice;
  button.append(letter, text);
  button.addEventListener('click', () => submitAnswer(index));
  return button;
}
function renderStudent() {
  if (!state || !state.you) return;
  $('studentNameBadge').textContent = `👋 ${state.you.name}`;
  $('studentCodeBadge').textContent = state.code;
  $('studentCount').textContent = state.participants;
  $('studentScore').textContent = number(state.you.score);
  const status = state.status;
  $('studentStateChip').textContent = status === 'live' ? '🟢 퀴즈 진행 중' : status === 'results' ? '🏆 정답 발표' : '🟡 문제 준비 중';
  $('studentStateChip').className = 'state-chip' + (status === 'live' ? ' live' : status === 'results' ? ' results' : '');
  $('studentQuestionCount').textContent = status === 'draft' ? (state.round_no ? '다음 문제 준비 중' : '문제 준비 중') : `${state.round_no}번 문제`;
  $('studentMultiplier').textContent = `✨ ${multiplierLabel(state.multiplier)} 문제`;
  $('studentQuestion').textContent = state.question.trim() || '선생님이 문제를 작성하고 있어요 ✏️';
  $('studentInstruction').textContent = status === 'live' ? '정답을 골라 주세요! 빠를수록 점수가 높아져요.' : status === 'results' ? '정답은 초록색으로 표시됩니다.' : '교사가 입력하는 내용이 여기에 바로 보입니다. 시작 전에는 답을 누를 수 없어요.';
  const box = $('studentChoices'); box.replaceChildren();
  state.options.forEach((choice, index) => {if (choice.trim()) box.appendChild(choiceButton(choice, index));});
  const note = $('studentResponseNote');
  note.className = 'response-note';
  if (status === 'draft') note.textContent = '⌛ 선생님이 「퀴즈 시작」을 누르면 선택할 수 있어요.';
  else if (status === 'live' && answerBusy) note.textContent = '답안을 제출하는 중이에요…';
  else if (status === 'live' && state.you.answer !== null) { note.textContent = `✅ ${letters[state.you.answer]}번 보기 제출 완료! 선생님의 종료를 기다려 주세요.`;note.classList.add('success'); }
  else if (status === 'live') note.textContent = `🏃 ${state.submitted}/${state.participants}명이 답을 제출했어요.`;
  else note.textContent = `정답: ${letters[state.correct_index]} · ${state.options[state.correct_index]}`;
  renderResults();
}
async function submitAnswer(index) {
  if (answerBusy || !state || state.status !== 'live' || state.you.answer !== null) return;
  answerBusy = true;renderStudent();
  try { await api(`/api/rooms/${roomCode}/answer`, {choice:index}, sessionToken); }
  catch (error) {showToast(error.message, true);}
  finally {answerBusy = false; if (state) renderStudent();}
}
async function initStudent() {
  showView('studentView');
  if (!roomCode) {location.replace('/');return;}
  sessionToken = localStorage.getItem(`toktok_student_${roomCode}`) || '';
  if (!sessionToken) {location.replace(`/join?code=${encodeURIComponent(roomCode)}`);return;}
  try {
    state = await getState(roomCode, sessionToken);
    if (state.role !== 'student') throw new Error('학생 입장 정보가 아닙니다.');
  } catch (error) {
    localStorage.removeItem(`toktok_student_${roomCode}`);
    showToast(error.message, true);
    setTimeout(() => location.replace(`/join?code=${encodeURIComponent(roomCode)}`), 1800);
    return;
  }
  renderStudent(); connectToRoom();
}
if (page === '/teacher') initTeacher();
else if (page === '/play') initStudent();
else initHome();
