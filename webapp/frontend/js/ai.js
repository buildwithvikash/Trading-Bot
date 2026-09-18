/**
 * Gold Trading Terminal — AI Copilot & Autonomous Quant Engine Controller.
 */

(function () {
  'use strict';

  let chatHistory = [];
  let isGenerating = false;

  // Simple Markdown Formatter
  function renderMarkdown(text) {
    if (!text) return '';
    let html = text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');

    // Code blocks
    html = html.replace(/```([\s\S]*?)```/g, function (match, p1) {
      return `<pre class="ai-code-block"><code>${p1.trim()}</code></pre>`;
    });

    // Inline code
    html = html.replace(/`([^`]+)`/g, '<code class="ai-inline-code">$1</code>');

    // Headers
    html = html.replace(/^### (.*$)/gim, '<h3 class="ai-h3">$1</h3>');
    html = html.replace(/^## (.*$)/gim, '<h2 class="ai-h2">$1</h2>');
    html = html.replace(/^# (.*$)/gim, '<h1 class="ai-h1">$1</h1>');

    // Bold & Italic
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');

    // Tables
    html = html.replace(/^\|(.+)\|$/gim, function (match) {
      const cells = match.split('|').slice(1, -1);
      const isHeader = match.includes('---');
      if (isHeader) return '';
      const tag = 'td';
      const cellHtml = cells.map(c => `<${tag}>${c.trim()}</${tag}>`).join('');
      return `<tr class="ai-tr">${cellHtml}</tr>`;
    });
    html = html.replace(/(<tr class="ai-tr">[\s\S]*?<\/tr>)/g, '<div class="table-scroll"><table class="ai-table">$1</table></div>');

    // Alerts
    html = html.replace(/> \[!NOTE\]\n> (.*)/g, '<div class="ai-alert ai-alert-note">ℹ️ $1</div>');
    html = html.replace(/> \[!IMPORTANT\]\n> (.*)/g, '<div class="ai-alert ai-alert-important">⚡ $1</div>');

    // Unordered lists
    html = html.replace(/^\s*[-*+]\s+(.*)$/gim, '<li class="ai-li">$1</li>');
    html = html.replace(/(<li class="ai-li">[\s\S]*?<\/li>)/g, '<ul class="ai-ul">$1</ul>');

    // Line breaks
    html = html.replace(/\n\n/g, '<br><br>');

    return html;
  }

  // Auto-scroll chat box to bottom
  function scrollToBottom(elem) {
    if (elem) {
      elem.scrollTop = elem.scrollHeight;
    }
  }

  // Stream text from AI chat endpoint
  async function streamPrompt(promptText, chatBoxElem, isAppend = false) {
    if (isGenerating) return;
    isGenerating = true;

    const userMsgElem = document.createElement('div');
    userMsgElem.className = 'ai-msg ai-msg-user';
    userMsgElem.innerHTML = `<div class="ai-msg-header">You</div><div class="ai-msg-body">${promptText}</div>`;
    chatBoxElem.appendChild(userMsgElem);

    const botMsgElem = document.createElement('div');
    botMsgElem.className = 'ai-msg ai-msg-bot';
    botMsgElem.innerHTML = `<div class="ai-msg-header"><span class="ai-badge">Claude AI</span></div><div class="ai-msg-body ai-typing"><span class="ai-dot-flashing"></span></div>`;
    chatBoxElem.appendChild(botMsgElem);
    scrollToBottom(chatBoxElem);

    const botBodyElem = botMsgElem.querySelector('.ai-msg-body');
    let fullText = '';

    try {
      const response = await fetch('/api/ai/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prompt: promptText,
          history: chatHistory.slice(-6),
          timeframe: '15min',
        }),
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      botBodyElem.classList.remove('ai-typing');
      botBodyElem.innerHTML = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value, { stream: true });
        fullText += chunk;
        botBodyElem.innerHTML = renderMarkdown(fullText);
        scrollToBottom(chatBoxElem);
      }

      chatHistory.push({ role: 'user', content: promptText });
      chatHistory.push({ role: 'assistant', content: fullText });

    } catch (err) {
      botBodyElem.classList.remove('ai-typing');
      botBodyElem.innerHTML = `<div class="risk-warning">Failed to stream AI response: ${err.message}</div>`;
    } finally {
      isGenerating = false;
    }
  }

  // Trigger Autonomous AI Quant Engine
  async function runAutoQuant() {
    const btn = document.getElementById('runAutoQuantBtn');
    const statusBox = document.getElementById('autoQuantStatus');
    const reportBox = document.getElementById('autoQuantReport');
    if (!btn || !statusBox || !reportBox) return;

    btn.disabled = true;
    btn.innerHTML = '<span class="ai-spinner"></span> Running Quant Engine…';
    statusBox.hidden = false;
    statusBox.className = 'ai-status-card active';
    statusBox.innerHTML = `
      <div class="ai-status-step">1. Analyzing XAU/USD price chart & indicators…</div>
      <div class="ai-status-step">2. Discovering & optimizing strategy rules with Claude Opus…</div>
      <div class="ai-status-step">3. Benchmarking candidates on 1-year historical data…</div>
      <div class="ai-status-step">4. Promoting qualified strategies into live Demo Trading…</div>
    `;

    try {
      const res = await fetch('/api/ai/auto-quant', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          timeframe: '15min',
          min_return_pct: 0.0,
          max_dd_pct: 25.0,
          auto_start_demo: true,
        }),
      });

      if (!res.ok) {
        const errJson = await res.json();
        throw new Error(errJson.detail || `HTTP ${res.status}`);
      }

      const data = await res.json();
      statusBox.className = 'ai-status-card success';
      statusBox.innerHTML = `✅ <strong>Autonomous Quant Execution Complete!</strong> Evaluated ${data.total_candidates} strategies, promoted <strong>${data.total_passed}</strong> to saved strategies & live demo trading in ${data.duration_sec}s.`;

      reportBox.hidden = false;
      reportBox.innerHTML = renderMarkdown(data.report_markdown);

      // Refresh strategy lists and demo views if loaded
      if (window.loadSavedStrategies) window.loadSavedStrategies();
      if (window.loadPaperStatus) window.loadPaperStatus();

    } catch (err) {
      statusBox.className = 'ai-status-card error';
      statusBox.innerHTML = `❌ <strong>Quant Engine Error:</strong> ${err.message}`;
    } finally {
      btn.disabled = false;
      btn.innerHTML = '✨ Run Autonomous AI Quant Engine';
    }
  }

  // Initialize Event Listeners
  function initAI() {
    const sendBtn = document.getElementById('aiSendBtn');
    const inputElem = document.getElementById('aiInput');
    const chatBox = document.getElementById('aiChatBox');
    const autoQuantBtn = document.getElementById('runAutoQuantBtn');
    const scanMarketBtn = document.getElementById('aiScanMarketBtn');

    if (sendBtn && inputElem && chatBox) {
      const handleSend = () => {
        const txt = inputElem.value.trim();
        if (txt) {
          inputElem.value = '';
          streamPrompt(txt, chatBox);
        }
      };

      sendBtn.addEventListener('click', handleSend);
      inputElem.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          handleSend();
        }
      });
    }

    if (autoQuantBtn) {
      autoQuantBtn.addEventListener('click', runAutoQuant);
    }

    if (scanMarketBtn && chatBox) {
      scanMarketBtn.addEventListener('click', () => {
        streamPrompt('Perform a quick high-level quantitative market structure scan for XAU/USD.', chatBox);
      });
    }

    const clearBtn = document.getElementById('aiClearBtn');
    if (clearBtn && chatBox) {
      clearBtn.addEventListener('click', () => {
        chatHistory = [];
        chatBox.innerHTML = `
          <div class="ai-msg ai-msg-bot">
            <div class="ai-msg-header"><span class="ai-badge">Claude AI</span></div>
            <div class="ai-msg-body">
              Hello! I am your <strong>Quantitative AI Assistant</strong> for the Gold Trading Terminal. Ask me anything about XAU/USD price action, risk management, or strategy optimization — or click <strong>"Run Autonomous AI Quant Engine"</strong> above to let me discover, backtest on 1-year data, and auto-trade new strategies for you!
            </div>
          </div>
        `;
      });
    }

    // Handle Quick Prompt Chips
    document.addEventListener('click', (e) => {
      if (e.target && e.target.classList.contains('ai-chip-btn') && e.target.id !== 'aiClearBtn') {
        const promptText = e.target.getAttribute('data-prompt');
        if (promptText && chatBox) {
          streamPrompt(promptText, chatBox);
        }
      }
    });
  }


  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAI);
  } else {
    initAI();
  }
})();
