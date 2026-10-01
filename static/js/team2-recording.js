(function () {
    'use strict';

    const CONFIG = Object.freeze({
        width: 640,
        height: 480,
        fps: 15,
        segmentMs: 5000,
        preEventMs: 10000,
        quietMs: 10000,
        uploadRetries: 3
    });

    const state = {
        sessionId: localStorage.getItem('session_id'),
        video: null,
        stream: null,
        recorder: null,
        mimeType: null,
        sequence: 0,
        segmentStartExamMs: 0,
        segmentChunks: [],
        segmentTimer: null,
        running: false,
        ending: false,
        restartInProgress: false,
        pendingUploads: new Set(),
        failedUploads: 0,
        examStartPerfMs: performance.now(),
        lastStatus: null
    };

    function examElapsedMs() {
        return Math.max(0, Math.round(performance.now() - state.examStartPerfMs));
    }

    function chooseMimeType() {
        const candidates = [
            'video/webm;codecs=vp9,opus',
            'video/webm;codecs=vp8,opus',
            'video/webm'
        ];
        return candidates.find(t => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || '';
    }

    function setStatus(message, level) {
        state.lastStatus = { message, level };
        if (typeof window.setStatus === 'function') {
            window.setStatus(message, level || 'ok');
        }
    }

    async function reportIssue(type, message) {
        try {
            await fetch('/api/team2/recording/issue', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    session_id: state.sessionId,
                    issue_type: type,
                    exam_elapsed_ms: examElapsedMs(),
                    message
                })
            });
        } catch (e) {
            console.warn('Team 2 issue report failed:', e);
        }
    }

    function clearSegmentTimer() {
        if (state.segmentTimer) {
            clearTimeout(state.segmentTimer);
            state.segmentTimer = null;
        }
    }

    async function uploadSegment(blob, metadata) {
        if (!blob || blob.size === 0) return false;
        let lastError = null;
        for (let attempt = 1; attempt <= CONFIG.uploadRetries; attempt++) {
            const form = new FormData();
            form.append('session_id', String(state.sessionId));
            form.append('sequence_no', String(metadata.sequenceNo));
            form.append('start_exam_ms', String(metadata.startExamMs));
            form.append('end_exam_ms', String(metadata.endExamMs));
            form.append('duration_ms', String(metadata.durationMs));
            form.append('mime_type', state.mimeType || blob.type || 'video/webm');
            form.append('segment', blob, `segment_${metadata.sequenceNo}.webm`);
            try {
                const response = await fetch('/api/team2/recording/segment', { method: 'POST', body: form });
                if (response.ok) return true;
                lastError = new Error(await response.text());
            } catch (err) {
                lastError = err;
            }
            await new Promise(r => setTimeout(r, attempt * 700));
        }
        state.failedUploads += 1;
        await reportIssue('SEGMENT_UPLOAD_FAILURE', String(lastError || 'segment upload failed'));
        setStatus('Recording upload delayed — retrying', 'warn');
        return false;
    }

    function queueSegmentUpload(blob, metadata) {
        const promise = uploadSegment(blob, metadata)
            .finally(() => state.pendingUploads.delete(promise));
        state.pendingUploads.add(promise);
        return promise;
    }

    function scheduleStop() {
        clearSegmentTimer();
        if (!state.running || state.ending || !state.recorder) return;
        state.segmentTimer = setTimeout(() => {
            if (state.recorder && state.recorder.state === 'recording') {
                state.recorder.stop();
            }
        }, CONFIG.segmentMs);
    }

    function createRecorder() {
        if (!state.stream) throw new Error('Camera stream is not available');
        const options = state.mimeType ? { mimeType: state.mimeType, videoBitsPerSecond: 900000, audioBitsPerSecond: 64000 } : undefined;
        const recorder = new MediaRecorder(state.stream, options);
        state.recorder = recorder;
        state.segmentChunks = [];
        state.segmentStartExamMs = examElapsedMs();

        recorder.ondataavailable = event => {
            if (event.data && event.data.size > 0) state.segmentChunks.push(event.data);
        };

        recorder.onerror = async event => {
            await reportIssue('RECORDER_ERROR', event.error ? event.error.message : 'MediaRecorder error');
            setStatus('Recording interrupted — attempting recovery', 'warn');
            restartRecorder();
        };

        recorder.onstop = () => {
            const endExamMs = examElapsedMs();
            const blob = new Blob(state.segmentChunks, { type: state.mimeType || 'video/webm' });
            const metadata = {
                sequenceNo: state.sequence++,
                startExamMs: state.segmentStartExamMs,
                endExamMs,
                durationMs: Math.max(1, endExamMs - state.segmentStartExamMs)
            };
            state.segmentChunks = [];
            if (!state.ending && !state.restartInProgress) {
                try {
                    createRecorder().start();
                    scheduleStop();
                } catch (e) {
                    reportIssue('RECORDER_RESTART_FAILURE', e.message);
                    setStatus('Recording unavailable — attempting camera recovery', 'error');
                    restartCamera();
                }
            }
            queueSegmentUpload(blob, metadata);
        };

        return recorder;
    }

    function startRecorder() {
        const recorder = createRecorder();
        recorder.start();
        state.running = true;
        scheduleStop();
        setStatus('Camera and recording active', 'ok');
    }

    async function restartRecorder() {
        if (state.restartInProgress || state.ending) return;
        state.restartInProgress = true;
        try {
            clearSegmentTimer();
            if (state.recorder && state.recorder.state === 'recording') state.recorder.stop();
            await new Promise(r => setTimeout(r, 150));
            if (state.stream && state.recorder && state.recorder.state === 'inactive') {
                startRecorder();
            }
        } catch (e) {
            await reportIssue('RECORDER_RESTART_FAILURE', e.message);
        } finally {
            state.restartInProgress = false;
        }
    }

    async function acquireCamera() {
        const stream = await navigator.mediaDevices.getUserMedia({
            video: { width: { ideal: CONFIG.width }, height: { ideal: CONFIG.height }, frameRate: { ideal: CONFIG.fps, max: CONFIG.fps } },
            audio: true
        });
        state.stream = stream;
        window.currentStream = stream;
        if (state.video) {
            state.video.srcObject = stream;
            await state.video.play();
        }
        attachTrackHealth(stream);
        return stream;
    }

    async function restartCamera() {
        if (state.restartInProgress || state.ending) return;
        state.restartInProgress = true;
        setStatus('Camera interrupted — reconnecting…', 'warn');
        try {
            if (state.recorder && state.recorder.state === 'recording') state.recorder.stop();
            if (state.stream) state.stream.getTracks().forEach(track => track.stop());
            await acquireCamera();
            await new Promise(r => setTimeout(r, 100));
            if (state.recorder && state.recorder.state === 'inactive') {
                startRecorder();
            } else if (!state.recorder) {
                startRecorder();
            }
            setStatus('Camera and recording recovered', 'ok');
        } catch (e) {
            await reportIssue('CAMERA_RECOVERY_FAILURE', e.message);
            setStatus('Recording unavailable — exam continues; please check camera access', 'error');
        } finally {
            state.restartInProgress = false;
        }
    }

    function attachTrackHealth(stream) {
        const videoTrack = stream.getVideoTracks()[0];
        const audioTrack = stream.getAudioTracks()[0];
        if (videoTrack) {
            videoTrack.addEventListener('ended', () => restartCamera());
            videoTrack.addEventListener('mute', () => {
                setStatus('Camera temporarily muted — recovering…', 'warn');
                restartCamera();
            });
        }
        if (audioTrack) {
            audioTrack.addEventListener('ended', () => reportIssue('AUDIO_TRACK_ENDED', 'Audio track ended'));
        }
    }

    async function waitForUploads() {
        while (state.pendingUploads.size) {
            await Promise.allSettled(Array.from(state.pendingUploads));
        }
    }

    async function endExam() {
        if (state.ending) return;
        state.ending = true;
        clearSegmentTimer();
        if (state.recorder && state.recorder.state === 'recording') state.recorder.stop();
        await waitForUploads();
        if (window.Team2IncidentManager) await window.Team2IncidentManager.finalizeActive({ forceEnd: true, examEndMs: examElapsedMs() });
        if (state.stream) state.stream.getTracks().forEach(track => track.stop());
        state.running = false;
    }

    async function init(videoElement) {
        state.video = videoElement;
        state.mimeType = chooseMimeType();
        if (!window.MediaRecorder || !state.mimeType) {
            setStatus('Browser recording is unavailable — exam continues', 'error');
            await reportIssue('RECORDING_UNSUPPORTED', 'MediaRecorder/WebM is not supported');
            return false;
        }
        try {
            await acquireCamera();
            startRecorder();
            return true;
        } catch (e) {
            setStatus('Camera error — attempting recovery', 'error');
            await reportIssue('CAMERA_INIT_FAILURE', e.message);
            return false;
        }
    }

    window.Team2Recording = {
        init,
        endExam,
        getExamElapsedMs: examElapsedMs,
        reportIssue,
        getConfig: () => ({ ...CONFIG })
    };
})();
