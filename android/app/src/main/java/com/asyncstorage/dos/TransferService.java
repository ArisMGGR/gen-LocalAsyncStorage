package com.asyncstorage.dos;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.IBinder;
import android.provider.DocumentsContract;
import android.provider.OpenableColumns;
import android.widget.Toast;

import androidx.annotation.Nullable;
import androidx.core.app.NotificationCompat;
import androidx.documentfile.provider.DocumentFile;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.security.MessageDigest;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * A foreground service that runs uploads and downloads so they continue when the
 * app is in the background, showing an ongoing progress notification. Each request
 * is delivered via startForegroundService with an action + extras; requests queue
 * on a single worker thread, and the service stops once the queue drains.
 */
public class TransferService extends Service {
    public static final String ACTION_DOWNLOAD = "com.asyncstorage.dos.DOWNLOAD";
    public static final String ACTION_UPLOAD = "com.asyncstorage.dos.UPLOAD";
    // A download names each file's server path and where it goes in the download
    // folder; both were worked out (and name clashes settled) when the user asked.
    public static final String EXTRA_PATHS = "paths";         // server paths
    public static final String EXTRA_DESTS = "dests";         // relative to the download folder
    public static final String EXTRA_OVERWRITE = "overwrite"; // boolean per file
    public static final String EXTRA_SIZES = "sizes";         // per-file byte counts
    public static final String EXTRA_DIRS = "dirs";           // folders to create first
    public static final String EXTRA_LABEL = "label";         // shown in the Queue
    public static final String EXTRA_URIS = "uris";
    public static final String EXTRA_DEST_DIR = "dest_dir";

    // Progress broadcast so a visible Activity can mirror the notification.
    public static final String PROGRESS = "com.asyncstorage.dos.TRANSFER_PROGRESS";
    public static final String P_LABEL = "label";
    public static final String P_PCT = "pct";
    public static final String P_SPEED = "speed";
    public static final String P_FINISHED = "finished";

    // Queue: the app can view waiting transfers and cancel them by id.
    public static final String QUEUE_UPDATE = "com.asyncstorage.dos.QUEUE_UPDATE";
    public static final String Q_LABELS = "q_labels";
    public static final String Q_IDS = "q_ids";
    public static final String Q_CURRENT_ID = "q_current_id";   // id of the running transfer, or -1
    public static final String Q_CLAIMED = "q_claimed";         // server paths queued or downloading
    public static final String ACTION_CANCEL_QUEUED = "com.asyncstorage.dos.CANCEL_QUEUED";
    public static final String ACTION_QUERY_QUEUE = "com.asyncstorage.dos.QUERY_QUEUE";
    public static final String EXTRA_ID = "id";

    private static final String CHANNEL = "transfers";
    private static final int NOTIF_ID = 1001;
    private static final int BUF = 1 << 16;

    private ExecutorService exec;
    private NotificationManager nm;
    private final AtomicInteger inFlight = new AtomicInteger(0);

    /** One queued transfer request; cancellable while still waiting. */
    private static final class Task {
        final int id;
        final Intent intent;
        final String label;
        volatile boolean cancelled;
        Task(int id, Intent intent, String label) {
            this.id = id;
            this.intent = intent;
            this.label = label;
        }
    }

    private final java.util.LinkedList<Task> pending = new java.util.LinkedList<>();
    private final AtomicInteger idGen = new AtomicInteger(0);
    private volatile Task current;  // the task being processed (shown as a running Task, not queued)

    @Override
    public void onCreate() {
        super.onCreate();
        exec = Executors.newSingleThreadExecutor();
        nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL, getString(R.string.notif_channel), NotificationManager.IMPORTANCE_LOW);
            ch.setShowBadge(false);
            nm.createNotificationChannel(ch);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) {
            stopSelf(startId);
            return START_NOT_STICKY;
        }
        String action = intent.getAction();

        // Cancel a still-waiting task by id (no effect once it's running).
        if (ACTION_CANCEL_QUEUED.equals(action)) {
            int id = intent.getIntExtra(EXTRA_ID, -1);
            synchronized (pending) {
                // Waiting task → drop it; the running task → flag it to stop after the
                // current file (loops check task.cancelled between files).
                Task cur = current;
                if (cur != null && cur.id == id) {
                    cur.cancelled = true;
                } else {
                    for (java.util.Iterator<Task> it = pending.iterator(); it.hasNext(); ) {
                        Task t = it.next();
                        if (t.id == id) { t.cancelled = true; it.remove(); break; }
                    }
                }
            }
            broadcastQueue();
            if (inFlight.get() <= 0) stopSelf(startId);
            return START_NOT_STICKY;
        }
        // Re-send the current queue (e.g. the Activity just resumed).
        if (ACTION_QUERY_QUEUE.equals(action)) {
            broadcastQueue();
            if (inFlight.get() <= 0) stopSelf(startId);
            return START_NOT_STICKY;
        }

        // Asking for a file that's already queued or downloading does nothing —
        // tapping Get twice used to fetch the same file twice, one after the other.
        if (ACTION_DOWNLOAD.equals(action)) {
            Intent trimmed = withoutClaimed(intent);
            if (trimmed == null) {
                Toast.makeText(this, R.string.already_queued, Toast.LENGTH_SHORT).show();
                broadcastQueue();
                if (inFlight.get() <= 0) stopSelf(startId);
                return START_NOT_STICKY;
            }
            intent = trimmed;
        }

        // A transfer request: enqueue it and process on the single worker thread.
        final Task task = new Task(idGen.incrementAndGet(), intent, labelFor(intent));
        synchronized (pending) { pending.add(task); }
        inFlight.incrementAndGet();
        broadcastQueue();
        startForegroundCompat(buildOngoing(getString(R.string.notif_starting), 0, false));
        exec.execute(() -> runTask(task));
        return START_NOT_STICKY;
    }

    /** Process one queued task (on the worker thread), keeping the queue broadcast current. */
    private void runTask(Task task) {
        synchronized (pending) {
            pending.remove(task);
            current = task;
        }
        broadcastQueue();  // it's no longer waiting — it's running
        try {
            if (!task.cancelled) handle(task);
        } catch (Exception ignore) {
            // per-item failures already notify; never crash the service
        } finally {
            synchronized (pending) { if (current == task) current = null; }
            broadcastQueue();
            if (inFlight.decrementAndGet() <= 0) {
                broadcast(null, 0, null, true);
                stopForeground(Service.STOP_FOREGROUND_REMOVE);
                stopSelf();
            }
        }
    }

    /** Broadcast the labels + ids of the waiting tasks (the running one is excluded). */
    private void broadcastQueue() {
        String[] labels;
        int[] ids;
        synchronized (pending) {
            int n = pending.size();
            labels = new String[n];
            ids = new int[n];
            int i = 0;
            for (Task t : pending) {
                labels[i] = t.label;
                ids[i] = t.id;
                i++;
            }
        }
        Task cur = current;
        Intent i = new Intent(QUEUE_UPDATE).setPackage(getPackageName());
        i.putExtra(Q_LABELS, labels);
        i.putExtra(Q_IDS, ids);
        i.putExtra(Q_CURRENT_ID, cur != null ? cur.id : -1);
        i.putExtra(Q_CLAIMED, claimedPaths().toArray(new String[0]));
        sendBroadcast(i);
    }

    /** The server paths a download request would fetch. */
    private static String[] pathsOf(Intent intent) {
        String[] paths = intent.getStringArrayExtra(EXTRA_PATHS);
        return paths == null ? new String[0] : paths;
    }

    /** Every path a queued or in-flight download already covers. */
    private java.util.Set<String> claimedPaths() {
        java.util.Set<String> claimed = new java.util.HashSet<>();
        synchronized (pending) {
            for (Task t : pending) {
                if (ACTION_DOWNLOAD.equals(t.intent.getAction())) {
                    java.util.Collections.addAll(claimed, pathsOf(t.intent));
                }
            }
        }
        Task cur = current;
        if (cur != null && ACTION_DOWNLOAD.equals(cur.intent.getAction())) {
            java.util.Collections.addAll(claimed, pathsOf(cur.intent));
        }
        claimed.remove(null);
        return claimed;
    }

    /**
     * The same download request with anything already queued or running removed,
     * the original when there was no overlap, or {@code null} when every file in
     * it is already covered (so the request can simply be dropped).
     */
    private Intent withoutClaimed(Intent intent) {
        java.util.Set<String> claimed = claimedPaths();
        if (claimed.isEmpty()) return intent;
        String[] paths = pathsOf(intent);
        String[] dests = intent.getStringArrayExtra(EXTRA_DESTS);
        boolean[] overwrite = intent.getBooleanArrayExtra(EXTRA_OVERWRITE);
        long[] sizes = intent.getLongArrayExtra(EXTRA_SIZES);

        java.util.List<Integer> keep = new java.util.ArrayList<>();
        for (int i = 0; i < paths.length; i++) {
            if (paths[i] != null && !claimed.contains(paths[i])) keep.add(i);
        }
        if (keep.isEmpty()) return null;
        if (keep.size() == paths.length) return intent;  // nothing overlapped

        int n = keep.size();
        String[] kp = new String[n];
        String[] kd = new String[n];
        boolean[] ko = new boolean[n];
        long[] ks = new long[n];
        for (int j = 0; j < n; j++) {
            int i = keep.get(j);
            kp[j] = paths[i];
            kd[j] = dests != null && i < dests.length ? dests[i] : null;
            ko[j] = overwrite != null && i < overwrite.length && overwrite[i];
            ks[j] = sizes != null && i < sizes.length ? sizes[i] : 0;
        }
        return new Intent(intent)
                .putExtra(EXTRA_PATHS, kp)
                .putExtra(EXTRA_DESTS, kd)
                .putExtra(EXTRA_OVERWRITE, ko)
                .putExtra(EXTRA_SIZES, ks);
    }

    /** A short human label for a queued request, shown in the app's Queue panel. */
    private String labelFor(Intent intent) {
        String action = intent.getAction();
        if (ACTION_UPLOAD.equals(action)) {
            String[] uris = intent.getStringArrayExtra(EXTRA_URIS);
            return getString(R.string.q_upload, uris == null ? 0 : uris.length);
        }
        String label = intent.getStringExtra(EXTRA_LABEL);
        if (label != null) return label;
        String[] paths = pathsOf(intent);
        return getString(R.string.q_download, paths.length > 0 ? baseName(paths[0]) : "");
    }

    private void handle(Task task) {
        Intent intent = task.intent;
        Prefs prefs = new Prefs(this);
        StorageApi api = new StorageApi(prefs.serverUrl(), prefs.apiKey());
        String action = intent.getAction();

        if (ACTION_DOWNLOAD.equals(action)) {
            SafStore store = new SafStore(this, prefs.downloadTreeUri());
            String[] dirs = intent.getStringArrayExtra(EXTRA_DIRS);
            if (dirs != null) {
                // Folders first, so a folder's empty sub-folders come along too.
                for (String dir : dirs) {
                    try {
                        store.mkdirs(dir);
                    } catch (Exception e) {
                        notifyDone(getString(R.string.notif_failed, dir + ": " + e.getMessage()));
                    }
                }
            }
            downloadBatch(api, store, intent, task);
        } else if (ACTION_UPLOAD.equals(action)) {
            String[] uris = intent.getStringArrayExtra(EXTRA_URIS);
            if (uris == null) return;
            // Land uploads inside the folder the user is viewing ("" = root).
            String destDir = intent.getStringExtra(EXTRA_DEST_DIR);
            for (String u : uris) {
                if (task.cancelled) break;  // finish the current file, then stop
                Uri uri = Uri.parse(u);
                String name = displayName(this, uri);
                String label = getString(R.string.notif_uploading, name);
                File tmp = null;
                try {
                    tmp = File.createTempFile("upload", ".part", getCacheDir());
                    String sha = copyAndHash(uri, tmp);
                    double mtime = lastModified(this, uri);
                    String target = (destDir == null || destDir.isEmpty())
                            ? SafStore.normalize(name)
                            : SafStore.normalize(destDir + "/" + name);
                    api.uploadFile(target, tmp, sha, mtime, progress(label), () -> task.cancelled);
                } catch (Exception e) {
                    // e.g. the server refusing a name that was taken meanwhile
                    if (!task.cancelled) {
                        notifyDone(getString(R.string.notif_failed, name + ": " + e.getMessage()));
                    }
                } finally {
                    if (tmp != null) tmp.delete();
                }
            }
        }
    }

    /**
     * Download a batch as one logical transfer: the notification/broadcast shows a
     * single progress bar over the batch's total bytes, so the user sees the real
     * overall progress instead of each small file racing 0→100% in turn.
     */
    private void downloadBatch(StorageApi api, SafStore store, Intent intent, Task task) {
        String[] paths = pathsOf(intent);
        String[] dests = intent.getStringArrayExtra(EXTRA_DESTS);
        boolean[] overwrite = intent.getBooleanArrayExtra(EXTRA_OVERWRITE);
        long[] sizes = intent.getLongArrayExtra(EXTRA_SIZES);
        long total = 0;
        if (sizes != null) {
            for (long s : sizes) if (s > 0) total += s;
        }
        final long grandTotal = total;       // 0 when sizes are unknown
        final int n = paths.length;
        // Throttle/EMA + running-offset state shared across every file in the batch.
        final int[] last = {-1};
        final long[] t0 = {System.nanoTime()};
        final long[] b0 = {0};
        final double[] ema = {0};
        final long[] base = {0};             // bytes fully accounted for by finished files

        for (int idx = 0; idx < n; idx++) {
            if (task.cancelled) break;  // finish the current file, then stop the rest
            final String path = paths[idx];
            if (path == null) continue;
            String dest = (dests != null && idx < dests.length && dests[idx] != null)
                    ? dests[idx] : baseName(path);
            boolean replace = overwrite != null && idx < overwrite.length && overwrite[idx];
            long size = (sizes != null && idx < sizes.length && sizes[idx] > 0) ? sizes[idx] : 0;
            final String label = n == 1
                    ? getString(R.string.notif_downloading, baseName(path))
                    : getString(R.string.notif_downloading_n, baseName(path), idx + 1, n);

            StorageApi.ProgressListener pl;
            if (grandTotal > 0) {
                pl = (done, fileTotal) -> {
                    long overall = base[0] + done;
                    int pct = (int) Math.min(100, overall * 100 / grandTotal);
                    if (pct != last[0]) {
                        long now = System.nanoTime();
                        double dt = (now - t0[0]) / 1e9;
                        long db = overall - b0[0];
                        double inst = dt > 0 ? db / dt : 0;
                        ema[0] = ema[0] == 0 ? inst : 0.3 * inst + 0.7 * ema[0];
                        last[0] = pct;
                        t0[0] = now;
                        b0[0] = overall;
                        String speed = Fmt.speed(ema[0]);
                        nm.notify(NOTIF_ID, buildOngoing(label + "  " + pct + "%  " + speed, pct, true));
                        broadcast(label, pct, speed, false);
                    }
                };
            } else {
                // Sizes unavailable: fall back to per-file progress.
                pl = progress(label);
            }

            downloadOne(api, store, path, dest, replace, pl, task);
            base[0] += size;                 // keep the bar monotonic even if a file failed
        }
    }

    /**
     * Download one file: stream it into a ".part" beside its destination (the
     * sha256 is checked on the way), then put it in place. A stopped or failed
     * download never leaves a partial file behind.
     */
    private void downloadOne(StorageApi api, SafStore store, String path, String dest,
                             boolean overwrite, StorageApi.ProgressListener pl, Task task) {
        DocumentFile tmp = null;
        try {
            tmp = store.beginWrite(dest);
            try (OutputStream out = getContentResolver().openOutputStream(tmp.getUri())) {
                if (out == null) throw new Exception("cannot open destination");
                api.downloadToStream(path, out, pl, () -> task.cancelled);
            }
            store.commit(tmp, dest, overwrite);
            tmp = null;
        } catch (Exception e) {
            // A cancel isn't a failure — the partial is discarded, no "Failed" notice.
            if (!task.cancelled) {
                notifyDone(getString(R.string.notif_failed, baseName(path) + ": " + e.getMessage()));
            }
        } finally {
            if (tmp != null) {
                try { tmp.delete(); } catch (Exception ignore) {}  // discard the partial file
            }
        }
    }

    /** Progress listener that updates the ongoing notification + broadcasts, throttled. */
    private StorageApi.ProgressListener progress(String label) {
        final int[] last = {-1};
        final long[] t0 = {System.nanoTime()};
        final long[] b0 = {0};
        final double[] ema = {0};
        return (done, total) -> {
            int pct = total > 0 ? (int) (done * 100 / total) : 0;
            if (pct != last[0]) {
                long now = System.nanoTime();
                double dt = (now - t0[0]) / 1e9;
                long db = done - b0[0];
                double inst = dt > 0 ? db / dt : 0;
                ema[0] = ema[0] == 0 ? inst : 0.3 * inst + 0.7 * ema[0];
                last[0] = pct;
                t0[0] = now;
                b0[0] = done;
                String speed = Fmt.speed(ema[0]);
                nm.notify(NOTIF_ID, buildOngoing(label + "  " + pct + "%  " + speed, pct, true));
                broadcast(label, pct, speed, false);
            }
        };
    }

    private Notification buildOngoing(String text, int pct, boolean showBar) {
        NotificationCompat.Builder b = new NotificationCompat.Builder(this, CHANNEL)
                .setSmallIcon(android.R.drawable.stat_sys_download)
                .setContentTitle(getString(R.string.app_name))
                .setContentText(text)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setPriority(NotificationCompat.PRIORITY_LOW);
        if (showBar) b.setProgress(100, pct, false);
        return b.build();
    }

    private void notifyDone(String text) {
        Notification n = new NotificationCompat.Builder(this, CHANNEL)
                .setSmallIcon(android.R.drawable.stat_sys_download_done)
                .setContentTitle(getString(R.string.app_name))
                .setContentText(text)
                .setAutoCancel(true)
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .build();
        nm.notify((int) (System.nanoTime() & 0x7fffffff), n);
    }

    private void startForegroundCompat(Notification n) {
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(NOTIF_ID, n);
        }
    }

    private void broadcast(String label, int pct, String speed, boolean finished) {
        Intent i = new Intent(PROGRESS).setPackage(getPackageName());
        i.putExtra(P_LABEL, label);
        i.putExtra(P_PCT, pct);
        i.putExtra(P_SPEED, speed);
        i.putExtra(P_FINISHED, finished);
        sendBroadcast(i);
    }

    private String copyAndHash(Uri uri, File tmp) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        try (InputStream in = getContentResolver().openInputStream(uri);
             OutputStream out = new FileOutputStream(tmp)) {
            if (in == null) throw new Exception("cannot open " + uri);
            byte[] buf = new byte[BUF];
            int n;
            while ((n = in.read(buf)) >= 0) {
                out.write(buf, 0, n);
                md.update(buf, 0, n);
            }
        }
        return Hashing.hex(md.digest());
    }

    /**
     * The picked document's last-modified time in seconds, as the desktop app
     * sends a file's mtime; the current time if the provider doesn't report one.
     */
    static double lastModified(Context ctx, Uri uri) {
        long millis = 0;
        try (Cursor c = ctx.getContentResolver().query(uri, null, null, null, null)) {
            if (c != null && c.moveToFirst()) {
                int idx = c.getColumnIndex(DocumentsContract.Document.COLUMN_LAST_MODIFIED);
                if (idx >= 0 && !c.isNull(idx)) millis = c.getLong(idx);
            }
        } catch (Exception ignore) {
        }
        long now = System.currentTimeMillis();
        // The server refuses a time far in the future, so a bogus one falls back too.
        if (millis <= 0 || millis > now) millis = now;
        return millis / 1000.0;
    }

    /** Display name of a content Uri (shared with the Activity's upload preview). */
    public static String displayName(Context ctx, Uri uri) {
        String name = null;
        try (Cursor c = ctx.getContentResolver().query(uri, null, null, null, null)) {
            if (c != null && c.moveToFirst()) {
                int idx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (idx >= 0) name = c.getString(idx);
            }
        } catch (Exception ignore) {
        }
        if (name == null || name.isEmpty()) name = uri.getLastPathSegment();
        if (name == null || name.isEmpty()) name = "upload-" + System.currentTimeMillis();
        return name;
    }

    private static String baseName(String path) {
        if (path == null) return "";
        String p = path.replace('\\', '/');
        int i = p.lastIndexOf('/');
        return i >= 0 ? p.substring(i + 1) : p;
    }

    @Nullable
    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        if (exec != null) exec.shutdownNow();
    }
}
