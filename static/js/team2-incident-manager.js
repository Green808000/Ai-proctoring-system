(function () {
    'use strict';

    const QUIET_MS = 10000;
    const state = {
        sessionId: localStorage.getItem('session_id'),
        activeIncidentId: null,
        lastEventMs: null,
        closeTimer: null
    };

    function acceptEvent(event) {
        if (!event || !event.incident_id) return;
        state.activeIncidentId = event.incident_id;
        state.lastEventMs = event.exam_elapsed_ms ?? state.lastEventMs;
        resetCloseTimer();
    }

    function acceptEvents(events) {
        if (!Array.isArray(events)) return;
        events.forEach(acceptEvent);
    }

    function resetCloseTimer() {
        if (state.closeTimer) clearTimeout(state.closeTimer);
        if (!state.activeIncidentId) return;
        state.closeTimer = setTimeout(() => finalizeActive(), QUIET_MS);
    }

    async function finalizeActive(options = {}) {
        if (!state.activeIncidentId) return null;
        const incidentId = state.activeIncidentId;
        state.activeIncidentId = null;
        if (state.closeTimer) {
            clearTimeout(state.closeTimer);
            state.closeTimer = null;
        }
        try {
            const response = await fetch(`/api/team2/incidents/${incidentId}/finalize`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    force_end: Boolean(options.forceEnd),
                    exam_end_ms: options.examEndMs ?? null
                })
            });
            if (response.status === 409) {
                state.activeIncidentId = incidentId;
                state.closeTimer = setTimeout(() => finalizeActive(options), 1500);
                return null;
            }
            if (!response.ok) throw new Error(await response.text());
            return await response.json();
        } catch (err) {
            console.error('Team 2 incident finalization failed:', err);
            if (window.Team2Recording) window.Team2Recording.reportIssue('INCIDENT_FINALIZE_FAILURE', err.message);
            state.activeIncidentId = incidentId;
            state.closeTimer = setTimeout(() => finalizeActive(options), 2000);
            return null;
        }
    }

    window.Team2IncidentManager = {
        acceptEvent,
        acceptEvents,
        finalizeActive,
        getState: () => ({ ...state })
    };
})();
