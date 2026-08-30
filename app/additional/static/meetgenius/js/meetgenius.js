/**
 * MeetGenius 前端 JavaScript 功能
 * 處理文件上傳、進度追蹤、用戶交互等
 */

document.addEventListener('DOMContentLoaded', () => {

  // --- DOM Element References ---
  const uploadCard = document.getElementById('uploadCard');
  const fileInfoCard = document.getElementById('fileInfoCard');
  const processingCard = document.getElementById('processingCard');
  
  const uploadArea = document.getElementById('uploadArea');
  const fileInput = document.getElementById('fileInput');
  
  const fileNameEl = document.getElementById('fileName');
  const fileSizeEl = document.getElementById('fileSize');

  const uploadForm = document.getElementById('uploadForm');
  const cancelBtn = document.getElementById('cancelBtn');

  const progressText = document.getElementById('progressText');
  const progressBar = document.getElementById('progressBar');

  if (!uploadArea) return;

  // --- Utility Functions ---

  const fadeOut = (el) => {
    el.style.transition = 'opacity 0.3s ease-out';
    el.style.opacity = 0;
    setTimeout(() => {
        el.style.display = 'none';
    }, 300);
  };

  const fadeIn = (el) => {
    el.style.display = 'block';
    el.style.opacity = 0;
    el.style.transition = 'opacity 0.3s ease-in';
    setTimeout(() => {
        el.style.opacity = 1;
    }, 10);
  };
  
  const formatFileSize = (bytes) => {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`;
  };

  // --- UI State Functions ---
  
  const showFileInfo = (file) => {
    fileNameEl.textContent = file.name;
    fileSizeEl.textContent = formatFileSize(file.size);
    
    fadeOut(uploadCard);
    setTimeout(() => fadeIn(fileInfoCard), 300);

    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);
    fileInput.files = dataTransfer.files;
  };

  const resetToUploadState = () => {
    fadeOut(fileInfoCard);
    fadeOut(processingCard);
    setTimeout(() => fadeIn(uploadCard), 300);
    fileInput.value = '';
    
    progressBar.style.width = '1%';
    progressBar.style.minWidth = '20px';
    progressText.textContent = '準備中...';
  };

  const showProcessingState = () => {
      fadeOut(fileInfoCard);
      setTimeout(() => fadeIn(processingCard), 300);
  }

  // --- Core Logic Functions ---

  const handleFileSelect = (file) => {
    const allowedExtensions = ['.m4a', '.mp3', '.wav', '.flac', '.webm', '.mp4'];
    const fileExtension = '.' + file.name.split('.').pop().toLowerCase();

    if (!allowedExtensions.includes(fileExtension)) {
      alert('不支援的檔案格式。請選擇 M4A、MP3、WAV、FLAC、WEBM 或 MP4。');
      return;
    }
    const maxSize = 500 * 1024 * 1024; // 500MB，與後端 MAX_CONTENT_LENGTH 一致
    if (file.size > maxSize) {
      alert('檔案大小超過 500MB 限制。');
      return;
    }
    showFileInfo(file);
  };
  
  let pollingTimer; // 輪詢計時器
  let fakeTimer; // 假進度計時器
  let processingCompleted = false; // 完成旗標

  const startProcessing = (meetingId) => {
      processingCompleted = false;
      // 立即輪詢一次
      pollProgress(meetingId);
      // 每 2 秒輪詢一次
      pollingTimer = setInterval(() => pollProgress(meetingId), 2000);
      // 每 10 秒假進度前進 10%，最多到 90%
      if (fakeTimer) clearInterval(fakeTimer);
      fakeTimer = setInterval(() => {
        if (processingCompleted) return;
        const current = parseInt(progressBar.getAttribute('aria-valuenow')) || 0;
        if (current < 90) {
          const next = Math.min(90, current + 10);
          progressBar.style.width = next + '%';
          progressBar.setAttribute('aria-valuenow', next);
          if (!progressText.textContent || /準備中|等待|檢查|處理中/i.test(progressText.textContent)) {
            progressText.textContent = '處理中，請稍候...';
          }
        }
      }, 10000);
  };

  const pollProgress = async (meetingId) => {
      try {
          const resp = await fetch(`/additional/meetgenius/meetings/${meetingId}/progress`, { cache: 'no-store' });
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          const result = await resp.json();
          if (result.status !== 'success') throw new Error(result.message || '查詢失敗');
          const data = result.data || {};
          updateProgress(data, meetingId);
      } catch (e) {
          console.error('輪詢進度失敗:', e);
      }
  };

  const updateProgress = (data, meetingId) => {
      // 處理心跳訊息 - 不更新進度條，只更新狀態文字
      if (data.step === 'heartbeat') {
          progressText.textContent = data.message;
          // 為心跳訊息添加動畫效果
          progressText.style.opacity = '0.7';
          setTimeout(() => {
              progressText.style.opacity = '1';
          }, 500);
          return;
      }
      
      progressText.textContent = data.message;
      
      // 只有當進度值有效時才更新進度條
      if (data.progress >= 0) {
          const currentShown = parseInt(progressBar.getAttribute('aria-valuenow')) || 0;
          const nextShown = Math.max(currentShown, data.progress);
          progressBar.style.width = nextShown + '%';
          progressBar.setAttribute('aria-valuenow', nextShown);
      }
      
      const stepKey = data.step.toLowerCase();
      if (stepKey === 'completed' && !processingCompleted) {
          processingCompleted = true;
          if (pollingTimer) clearInterval(pollingTimer);
          if (fakeTimer) clearInterval(fakeTimer);
          // 完成時直接拉到 100%
          progressBar.style.width = '100%';
          progressBar.setAttribute('aria-valuenow', 100);
          
          setTimeout(() => {
              if (meetingId) {
                window.location.href = `/additional/meetgenius/meetings/${meetingId}`;
              }
          }, 1500);
      }
      if (stepKey === 'failed' && !processingCompleted) {
          processingCompleted = true;
          if (pollingTimer) clearInterval(pollingTimer);
          if (fakeTimer) clearInterval(fakeTimer);
          progressBar.classList.remove('bg-warning');
          progressBar.classList.add('bg-danger');
      }
  };

  // 檢查會議處理狀態的函數
  const checkMeetingStatus = async (meetingId) => {
      try {
          progressText.textContent = '正在檢查處理狀態...';
          
          const response = await fetch(`/additional/meetgenius/meetings/${meetingId}/status`);
          if (!response.ok) {
              throw new Error(`HTTP ${response.status}`);
          }
          
          const result = await response.json();
          
          if (result.status === 'success') {
              const meetingStatus = result.meeting_status;
              
              switch (meetingStatus) {
                  case 'completed':
                      progressText.textContent = '處理已完成！正在跳轉...';
                      progressBar.style.width = '100%';
                      progressBar.setAttribute('aria-valuenow', 100);
                      if (pollingTimer) clearInterval(pollingTimer);
                      if (fakeTimer) clearInterval(fakeTimer);
                      progressBar.classList.remove('bg-warning');
                      progressBar.classList.add('bg-success');
                      
                      setTimeout(() => {
                          window.location.href = `/additional/meetgenius/meetings/${meetingId}`;
                      }, 1500);
                      break;
                      
                  case 'failed':
                      progressText.textContent = `處理失敗: ${result.error_message || '未知錯誤'}`;
                      progressBar.classList.remove('bg-warning');
                      progressBar.classList.add('bg-danger');
                      if (pollingTimer) clearInterval(pollingTimer);
                      if (fakeTimer) clearInterval(fakeTimer);
                      break;
                      
                  case 'processing':
                      progressText.textContent = '處理仍在進行中，請耐心等待...';
                      
                      // 清除現有按鈕
                      const existingButtons = progressText.parentElement.querySelectorAll('button');
                      existingButtons.forEach(btn => btn.remove());
                      
                      // 自動輪詢已啟動
                      break;
                      
                  default:
                      progressText.textContent = `當前狀態: ${meetingStatus}`;
                      setTimeout(() => {
                          checkMeetingStatus(meetingId);
                      }, 5000);
              }
          } else {
              throw new Error(result.message || '狀態檢查失敗');
          }
          
      } catch (error) {
          console.error('Status check error:', error);
          progressText.textContent = `狀態檢查失敗: ${error.message}。請手動刷新頁面。`;
      }
  };

  // --- Event Listeners ---

  ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
    uploadArea.addEventListener(eventName, e => {
      e.preventDefault();
      e.stopPropagation();
    }, false);
  });
  
  ['dragenter', 'dragover'].forEach(eventName => {
    uploadArea.addEventListener(eventName, () => uploadArea.classList.add('dragover'), false);
  });
  
  ['dragleave', 'drop'].forEach(eventName => {
    uploadArea.addEventListener(eventName, () => uploadArea.classList.remove('dragover'), false);
  });

  uploadArea.addEventListener('drop', e => {
    const files = e.dataTransfer.files;
    if (files.length > 0) {
      handleFileSelect(files[0]);
    }
  });

  // 點擊拖放區即開啟檔案選擇器
  uploadArea.addEventListener('click', () => fileInput.click());
  
  fileInput.addEventListener('change', e => {
    if (e.target.files.length > 0) {
      handleFileSelect(e.target.files[0]);
    }
  });

  cancelBtn.addEventListener('click', resetToUploadState);
  
  uploadForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!fileInput.files.length) {
      alert('請選擇一個檔案');
      return;
    }
    
    showProcessingState();
    
    const formData = new FormData(uploadForm);
    
    try {
        const response = await fetch(uploadForm.action, {
            method: 'POST',
            body: formData,
            headers: { 'X-Requested-With': 'XMLHttpRequest' }
        });

        if (!response.ok) {
            const errorResult = await response.json().catch(() => ({ message: '伺服器發生未知錯誤' }));
            throw new Error(errorResult.message || `伺服器錯誤: ${response.status}`);
        }

        const result = await response.json();

        if (result.status === 'success') {
            startProcessing(result.meeting_id);
        } else {
            alert(result.message || '上傳失敗，請重試。');
            resetToUploadState();
        }
    } catch (error) {
        console.error('Upload error:', error);
        alert(`上傳時發生錯誤: ${error.message}`);
        resetToUploadState();
    }
  });

});