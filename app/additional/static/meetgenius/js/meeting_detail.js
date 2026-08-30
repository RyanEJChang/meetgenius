function checkStatus(meetingId) {
    // 發送請求到後端 API
    fetch(`/api/meeting/${meetingId}/status`)
        .then(response => {
            if (!response.ok) {
                throw new Error('Network response was not ok');
            }
            return response.json();
        })
        .then(data => {
            const currentStatusElement = document.getElementById('currentStatus');
            const currentStatus = currentStatusElement ? currentStatusElement.textContent.trim() : '';

            // 如果狀態從 '處理中' 變為 '已完成' 或 '失敗'，則重新載入頁面
            if (data.status === 'completed' || data.status === 'failed') {
                console.log(`Status changed to ${data.status}. Reloading page.`);
                // 停止輪詢並重新載入頁面
                if (window.statusInterval) {
                    clearInterval(window.statusInterval);
                }
                window.location.reload();
            } else {
                // 可選：在這裡更新更細緻的進度，如果後端 API 支援的話
                console.log(`Status is still ${data.status}.`);
            }
        })
        .catch(error => {
            console.error('Error fetching meeting status:', error);
            // 如果發生錯誤，停止輪詢以避免產生大量錯誤日誌
            if (window.statusInterval) {
                clearInterval(window.statusInterval);
            }
        });
}

document.addEventListener('DOMContentLoaded', function() {
    const meetingInfo = document.querySelector('.x_panel');
    if (!meetingInfo) return;

    // 從頁面某個元素讀取 meetingId 和 status
    const meetingId = document.body.dataset.meetingId;
    const initialStatus = document.body.dataset.meetingStatus;

    if (meetingId && (initialStatus === 'uploaded' || initialStatus === 'processing')) {
        console.log(`Initiating status check for meeting ${meetingId}`);
        // 每 5 秒檢查一次狀態
        window.statusInterval = setInterval(() => checkStatus(meetingId), 5000);
    }
}); 