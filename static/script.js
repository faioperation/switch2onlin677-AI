// ===== STATE =====
let currentUserId = null;
let conversations = [];
let pendingImageBase64 = null;

// ===== DOM ELEMENTS =====
const sidebar = document.getElementById('sidebar');
const conversationsList = document.getElementById('conversationsList');
const messagesDiv = document.getElementById('messages');
const userInput = document.getElementById('userInput');
const emptyState = document.getElementById('emptyState');

// ===== THEME MANAGEMENT ===== 
function initTheme() {
  const theme = localStorage.getItem('theme') || 'light';
  if (theme === 'dark') {
    document.body.classList.add('dark');
  }
  updateThemeButton();
}

function updateThemeButton() {
  const isDark = document.body.classList.contains('dark');
  const btn = document.getElementById('themeToggleBtn');
  if (btn) {
    btn.innerHTML = isDark ? '<i data-lucide="sun"></i>' : '<i data-lucide="moon"></i>';
    lucide.createIcons();
  }
}

function toggleTheme() {
  const isDark = document.body.classList.contains('dark');
  
  if (isDark) {
    document.body.classList.remove('dark');
    localStorage.setItem('theme', 'light');
  } else {
    document.body.classList.add('dark');
    localStorage.setItem('theme', 'dark');
  }
  
  updateThemeButton();
}

// Initialize Theme
initTheme();
lucide.createIcons();

// ===== UTILITY FUNCTIONS =====
function isArabic(text) {
  const arabicPattern = /[\u0600-\u06FF]/;
  return arabicPattern.test(text);
}

function scrollToBottom() {
  messagesDiv.scrollTop = messagesDiv.scrollHeight;
}

function updateEmptyState() {
  if (messagesDiv.children.length === 0) {
    emptyState.classList.remove('hidden');
  } else {
    emptyState.classList.add('hidden');
  }
}

function esc(text) {
  const d = document.createElement('div');
  d.textContent = String(text || '');
  return d.innerHTML;
}

function formatDate(isoString) {
  const date = new Date(isoString);
  const now = new Date();
  const diff = now - date;
  const days = Math.floor(diff / (1000 * 60 * 60 * 24));
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return `${days}d ago`;
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function buildProductCard(p) {
  const name = p.name || 'Product';
  const firstLetter = name.trim().charAt(0).toUpperCase() || 'P';
  
  return `
    <div class="product-card">
      <div class="product-visual">
        <div class="product-avatar">${esc(firstLetter)}</div>
      </div>
      <div class="card-info">
        <div class="card-header-row">
          <div class="card-name">${esc(name)}</div>
        </div>
        <div class="card-price-badge">${esc(p.price || 'N/A')}</div>
        <div class="card-desc">${esc(p.description || '')}</div>
      </div>
    </div>`;
}

function addMessage(role, content, products, messageId) {
  products = Array.isArray(products) ? products : [];

  const row = document.createElement('div');
  row.className = `msg-row ${role}`;
  if (messageId) row.dataset.messageId = messageId;

  if (role === 'user') {
    if (!content || !content.trim()) return;
    
    const group = document.createElement('div');
    group.className = 'msg-group';
    
    const bubble = document.createElement('div');
    bubble.className = 'user-bubble';
    bubble.innerHTML = esc(content).replace(/\n/g, '<br>');
    
    group.appendChild(bubble);
    row.appendChild(group);
    messagesDiv.appendChild(row);
  } else {
    if (content && content.trim()) {
      const textRow = document.createElement('div');
      textRow.className = 'msg-row bot';
      
      const group = document.createElement('div');
      group.className = 'msg-group';
      
      const bubble = document.createElement('div');
      bubble.className = 'bot-bubble';
      bubble.innerHTML = esc(content).replace(/\n/g, '<br>');

      if (isArabic(content)) {
        bubble.style.direction = 'rtl';
        bubble.style.textAlign = 'right';
      } else {
        bubble.style.direction = 'ltr';
        bubble.style.textAlign = 'left';
      }
      
      group.appendChild(bubble);
      textRow.appendChild(group);
      messagesDiv.appendChild(textRow);
    }
    
    for (let p of products) {
      const productRow = document.createElement('div');
      productRow.className = 'msg-row bot';
      if (isArabic(p.name)) {
         productRow.style.direction = 'rtl';
      }
      
      const group = document.createElement('div');
      group.className = 'msg-group';
      group.innerHTML = buildProductCard(p);
      
      productRow.appendChild(group);
      messagesDiv.appendChild(productRow);
    }
  }
  
  updateEmptyState();
  scrollToBottom();
}

function showTyping() {
  const row = document.createElement('div');
  row.className = 'msg-row bot';
  row.id = 'typing-row';
  
  const group = document.createElement('div');
  group.className = 'msg-group';
  group.innerHTML = '<div class="typing-bubble"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>';
  
  row.appendChild(group);
  messagesDiv.appendChild(row);
  scrollToBottom();
}

function hideTyping() {
  const el = document.getElementById('typing-row');
  if (el) el.remove();
}

async function loadHistory(userId) {
  if (!userId) return;
  currentUserId = userId;
  messagesDiv.innerHTML = '';
  showTyping();
  
  try {
    const res = await fetch(`/history/${userId}`);
    if (!res.ok) throw new Error('History fetch failed');
    const history = await res.json();
    hideTyping();
    history.forEach(msg => {
      addMessage(msg.role, msg.content, msg.products, msg.id);
    });
    
    updateEmptyState();
    
    document.querySelectorAll('.conversation-item').forEach(item => {
      if (item.dataset.userId === userId) {
        item.classList.add('active');
      } else {
        item.classList.remove('active');
      }
    });
  } catch (err) {
    hideTyping();
    updateEmptyState();
  }
}

async function sendMessage() {
  const text = userInput.value.trim();
  const imageBase64 = pendingImageBase64;
  
  if (!text && !imageBase64) return;
  userInput.value = '';
  removeImage(); // Clear preview after sending

  if (!currentUserId) {
    await newChat();
  }

  addMessage('user', text || (imageBase64 ? "Searched with image" : ""), [], 'temp');
  showTyping();

  try {
    const res = await fetch('/reply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ 
        user_id: currentUserId, 
        message: text || "Suggest products based on this image",
        image_url: imageBase64 
      }),
    });
    
    if (!res.ok) throw new Error('Reply failed');
    const data = await res.json();

    const tempUserMsg = document.querySelector(`[data-message-id="temp"]`);
    if (tempUserMsg) tempUserMsg.remove();

    hideTyping();
    addMessage('user', text || "📷 Image search", [], data.user_message_id);
    addMessage('bot', data.reply, data.products, data.assistant_message_id);

    loadConversations();
  } catch (err) {
    hideTyping();
    addMessage('bot', 'Sorry, something went wrong. Please try again.', [], null);
  }
}

async function deleteMessage(messageId) {
  if (!confirm('Delete this message?')) return;
  try {
    const res = await fetch(`/message/${messageId}`, { method: 'DELETE' });
    if (res.ok) {
      const msgRow = document.querySelector(`[data-message-id="${messageId}"]`);
      if (msgRow) msgRow.remove();
      loadConversations();
    }
  } catch (err) {
    console.error(err);
  }
}

async function deleteConversation(userId) {
  const result = await Swal.fire({
    title: 'هل انت متأكد؟',
    text: "هل تريد حذف هذه المحادثة نهائياً؟",
    icon: 'warning',
    showCancelButton: true,
    confirmButtonColor: '#dc3545',
    cancelButtonColor: '#6c757d',
    confirmButtonText: 'نعم، احذف',
    cancelButtonText: 'إلغاء',
    background: document.body.classList.contains('dark') ? '#020617' : '#fff',
    color: document.body.classList.contains('dark') ? '#f1f5f9' : '#0f172a',
  });

  if (!result.isConfirmed) return;

  try {
    const res = await fetch(`/history/${userId}`, { method: 'DELETE' });
    if (res.ok) {
      if (currentUserId === userId) {
        messagesDiv.innerHTML = '';
        currentUserId = null;
        updateEmptyState();
      }
      loadConversations();
    }
  } catch (err) {
    console.error(err);
  }
}

async function clearCurrentChat() {
  if (!currentUserId) return;
  
  const result = await Swal.fire({
    title: 'مسح المحادثة؟',
    text: "هلต้องการ مسح جميع الرسائل؟",
    icon: 'question',
    showCancelButton: true,
    confirmButtonColor: '#dc3545',
    cancelButtonColor: '#6c757d',
    confirmButtonText: 'نعم، امسح الكل',
    cancelButtonText: 'إلغاء',
    background: document.body.classList.contains('dark') ? '#020617' : '#fff',
    color: document.body.classList.contains('dark') ? '#f1f5f9' : '#0f172a',
  });

  if (!result.isConfirmed) return;

  try {
    const res = await fetch(`/history/${currentUserId}`, { method: 'DELETE' });
    if (res.ok) {
      messagesDiv.innerHTML = '';
      updateEmptyState();
      loadConversations();
    }
  } catch (err) {
    console.error(err);
  }
}

async function newChat() {
  const newId = crypto.randomUUID ? crypto.randomUUID() : 'user_' + Date.now() + '_' + Math.random().toString(36);
  currentUserId = newId;
  localStorage.setItem('shopbot_user_id', newId);
  messagesDiv.innerHTML = '';
  updateEmptyState();
  loadConversations();
  closeSidebar();
}

async function loadConversations() {
  try {
    const res = await fetch('/conversations');
    if (!res.ok) throw new Error('Failed to fetch conversations');
    conversations = await res.json();
    renderConversations();
  } catch (err) {
    console.error(err);
  }
}

function renderConversations() {
  conversationsList.innerHTML = '';
  for (const conv of conversations) {
    const item = document.createElement('div');
    item.className = 'conversation-item';
    if (isArabic(conv.title)) item.classList.add('rtl');
    item.dataset.userId = conv.user_id;
    if (conv.user_id === currentUserId) item.classList.add('active');
    
    item.innerHTML = `
      <div class="conversation-info" onclick="loadHistory('${conv.user_id}'); if(window.innerWidth<=768) closeSidebar();">
        <div class="conversation-title">${esc(conv.title)}</div>
        <div class="conversation-date">${formatDate(conv.last_updated)}</div>
      </div>
      <button class="delete-conv-btn" onclick="deleteConversation('${conv.user_id}')"><i data-lucide="trash-2" style="width: 16px; height: 16px;"></i></button>
    `;
    conversationsList.appendChild(item);
  }
  lucide.createIcons();
}

function toggleSidebar() {
  const isOpen = sidebar.classList.toggle('open');
  const overlay = document.getElementById('sidebarOverlay');
  if (overlay) overlay.classList.toggle('visible', isOpen);
  // Lock body scroll when sidebar is open on mobile
  if (window.innerWidth <= 768) {
    document.body.style.overflow = isOpen ? 'hidden' : '';
  }
}

function closeSidebar() {
  sidebar.classList.remove('open');
  document.body.style.overflow = '';
  const overlay = document.getElementById('sidebarOverlay');
  if (overlay) overlay.classList.remove('visible');
}

// ===== IMAGE HANDLING =====
function handleFileSelect(e) {
  const file = e.target.files[0];
  if (!file) return;

  const reader = new FileReader();
  reader.onload = function(event) {
    pendingImageBase64 = event.target.result;
    const preview = document.getElementById('imagePreview');
    const container = document.getElementById('imagePreviewContainer');
    if (preview && container) {
      preview.src = event.target.result;
      container.classList.remove('hidden');
    }
  };
  reader.readAsDataURL(file);
}

function removeImage() {
  pendingImageBase64 = null;
  const fileInput = document.getElementById('fileInput');
  const container = document.getElementById('imagePreviewContainer');
  if (fileInput) fileInput.value = '';
  if (container) container.classList.add('hidden');
}

// ===== EVENT LISTENERS =====
document.addEventListener('DOMContentLoaded', function() {
  const newChatBtn = document.getElementById('newChatBtn');
  const clearChatBtn = document.getElementById('clearCurrentChatBtn');
  const themeBtn = document.getElementById('themeToggleBtn');
  const menuBtn = document.getElementById('menuToggle');
  const infoBtn = document.getElementById('infoBtn');
  const attachBtn = document.getElementById('attachBtn');

  if (newChatBtn) newChatBtn.addEventListener('click', newChat);
  if (clearChatBtn) clearChatBtn.addEventListener('click', clearCurrentChat);
  if (themeBtn) themeBtn.addEventListener('click', toggleTheme);
  if (menuBtn) menuBtn.addEventListener('click', toggleSidebar);
  
  if (attachBtn) attachBtn.addEventListener('click', () => {
    document.getElementById('fileInput').click();
  });

  const fileInput = document.getElementById('fileInput');
  if (fileInput) fileInput.addEventListener('change', handleFileSelect);

  const removeImageBtn = document.getElementById('removeImageBtn');
  if (removeImageBtn) removeImageBtn.addEventListener('click', removeImage);

  if (infoBtn) infoBtn.addEventListener('click', () => {
      Swal.fire({
        title: 'ضفاف بوت | DhifafBot',
        text: 'مساعدك الذكي للتسوق',
        icon: 'info',
        background: document.body.classList.contains('dark') ? '#0f172a' : '#fff',
        color: document.body.classList.contains('dark') ? '#f1f5f9' : '#0f172a',
        confirmButtonColor: '#0ea5e9',
        confirmButtonText: 'حسناً',
        showClass: {
          popup: 'animate__animated animate__fadeInUp'
        },
        hideClass: {
          popup: 'animate__animated animate__fadeOutDown'
        },
        html: `
          <div style="text-align: right; direction: rtl; margin-top: 1rem; font-family: 'IBM Plex Sans Arabic', sans-serif;">
            <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 12px;">
              <span style="color: #0ea5e9;">✦</span> <span>ابحث عن المنتجات بسهولة باستخدام اللغة الطبيعية</span>
            </div>
            <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 12px;">
              <span style="color: #0ea5e9;">✦</span> <span>تحدث بشكل طبيعي واطلب أدق التفاصيل</span>
            </div>
            <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 12px;">
              <span style="color: #0ea5e9;">✦</span> <span>قم بتقديم الطلبات مباشرة عبر الدردشة</span>
            </div>
            <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 12px;">
              <span style="color: #0ea5e9;">✦</span> <span>دعم كامل للغات متعددة</span>
            </div>
            <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 12px;">
              <span style="color: #0ea5e9;">✦</span> <span>تصميم عصري وسريع الاستجابة</span>
            </div>
          </div>
          <p style="margin-top: 1.5rem; font-size: 0.8rem; opacity: 0.7;">v1.0.0</p>
        `,
        customClass: {
          container: 'swal-container',
          popup: 'swal-popup',
          title: 'swal-title',
          htmlContainer: 'swal-html'
        },
        backdrop: true,
        allowOutsideClick: true,
        allowEscapeKey: true
      });
    });

});

userInput.addEventListener('keypress', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

// Close sidebar on mobile when clicking outside
document.addEventListener('click', (e) => {
  if (window.innerWidth <= 768) {
    if (!sidebar.contains(e.target) && !e.target.closest('.menu-toggle')) {
      closeSidebar();
    }
  }
});

// ===== INITIAL LOAD =====
newChat(); // Always start fresh on load/refresh
loadConversations();

// Final UI Init
updateEmptyState();
