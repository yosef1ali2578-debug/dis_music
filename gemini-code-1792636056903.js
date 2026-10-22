// ضع رابط السيرفر العام هنا (مثلاً رابط VPS أو Render أو Ngrok) ليعمل مع أي شخص بدون لوكال
const BOT_SERVER_URL = "http://YOUR_SERVER_IP_OR_DOMAIN:8080/sync";

let currentVideoId = null;
let lastSyncTime = 0;
let isAttached = false;

function getVideoIdFromUrl() {
    const params = new URLSearchParams(window.location.search);
    return params.get("v");
}

function getUpcomingPlaylistVideoIds() {
    // استخراج الأغاني القادمة من قائمة التشغيل في واجهة YouTube Music
    const queueItems = document.querySelectorAll("ytmusic-player-queue-item");
    const ids = [];
    queueItems.forEach((item) => {
        const link = item.querySelector("a[href*='watch?v=']");
        if (link) {
            const urlParams = new URLSearchParams(link.href.split("?")[1]);
            const vid = urlParams.get("v");
            if (vid && !ids.includes(vid)) {
                ids.push(vid);
            }
        }
    });
    return ids;
}

async function sendSyncEvent(action, extraData = {}) {
    const video = document.querySelector("video");
    const videoId = getVideoIdFromUrl();
    if (!video || !videoId) return;

    const payload = {
        action: action,
        videoId: videoId,
        position: Math.floor(video.currentTime || 0),
        ...extraData
    };

    try {
        await fetch(BOT_SERVER_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
    } catch (err) {
        console.debug("DiscordBot Sync Error:", err);
    }
}

function attachVideoListeners() {
    const video = document.querySelector("video");
    if (!video || isAttached) return;
    isAttached = true;

    // 1. عند تقديم أو تأخير الأغنية على يوتيوب ميوزيك
    video.addEventListener("seeked", () => {
        const now = Date.now();
        if (now - lastSyncTime > 800) {
            lastSyncTime = now;
            sendSyncEvent("seek");
        }
    });

    // 2. عند إيقاف الأغنية مؤقتاً
    video.addEventListener("pause", () => {
        if (!video.seeking && video.currentTime < video.duration - 1) {
            sendSyncEvent("pause");
        }
    });

    // 3. عند استئناف التشغيل
    video.addEventListener("playing", () => {
        const vid = getVideoIdFromUrl();
        if (vid !== currentVideoId) {
            currentVideoId = vid;
            const playlist = getUpcomingPlaylistVideoIds();
            sendSyncEvent("track_change", { playlist: playlist });
        } else {
            sendSyncEvent("play");
        }
    });

    // 4. عند انتهاء الأغنية الحالية للانتقال للتالية في البلاي ليست فوراً
    video.addEventListener("ended", () => {
        setTimeout(() => {
            const nextVid = getVideoIdFromUrl();
            if (nextVid && nextVid !== currentVideoId) {
                currentVideoId = nextVid;
                const playlist = getUpcomingPlaylistVideoIds();
                sendSyncEvent("track_change", { playlist: playlist });
            }
        }, 1000);
    });
}

// مراقبة تغير الأغنية في صفحة YouTube Music
setInterval(() => {
    attachVideoListeners();
    const vid = getVideoIdFromUrl();
    if (vid && vid !== currentVideoId) {
        currentVideoId = vid;
        const playlist = getUpcomingPlaylistVideoIds();
        sendSyncEvent("track_change", { playlist: playlist });
    }
}, 1500);