/* Dashboard inactivity timeout. Intentionally not loaded on the live exam page. */
(function () {
    const IDLE_TIMEOUT_MS = 5 * 60 * 1000;
    const ACTIVITY_EVENTS = ["mousemove", "mousedown", "keydown", "touchstart", "scroll"];

    let idleTimer = null;

    function logout() {
        localStorage.removeItem("token");
        localStorage.removeItem("student_id");
        localStorage.removeItem("session_id");
        alert("You've been logged out due to inactivity. Please log in again.");
        window.location.href = "/";
    }

    function resetTimer() {
        if (idleTimer) clearTimeout(idleTimer);
        idleTimer = setTimeout(logout, IDLE_TIMEOUT_MS);
    }

    ACTIVITY_EVENTS.forEach(function (evt) {
        document.addEventListener(evt, resetTimer, { passive: true });
    });

    resetTimer();
})();
