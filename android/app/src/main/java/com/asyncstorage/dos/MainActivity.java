package com.asyncstorage.dos;

import android.Manifest;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.DocumentsContract;
import android.text.Editable;
import android.text.TextWatcher;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ImageButton;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.content.ContextCompat;
import androidx.recyclerview.widget.DividerItemDecoration;
import androidx.recyclerview.widget.LinearLayoutManager;
import androidx.recyclerview.widget.RecyclerView;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * The browser window: a live folder view of server files, with folder
 * navigation, New folder, Move, per-row and multi-select download into the
 * chosen folder, delete to Trash (with Restore / Delete forever / Empty Trash),
 * and uploads (into the current folder) via the document picker. Mirrors the
 * desktop client (client/gui.py).
 *
 * Nothing downloaded is tracked: the download folder is only looked at when a
 * download is asked for, to catch a name that's already taken.
 */
public class MainActivity extends AppCompatActivity implements FileAdapter.Listener {

    private static final long POLL_MS = 4000;

    private Prefs prefs;
    private SafStore store;
    private StorageApi api;
    private FileAdapter adapter;

    private TextView status;
    private TextView location;
    private EditText search;
    private ImageButton deleteBtn;
    private Button addBtn;
    private ImageButton refreshBtn;
    private Button openBtn;
    private Button upBtn;
    private Button newFolderBtn;
    private Button trashBtn;
    // Where the user was when they opened Trash, so "Back to files" returns there.
    private String cwdBeforeTrash = "";

    /** Top-level folder that deleted items are moved into (so a delete is reversible). */
    private static final String TRASH = Plans.TRASH;

    // Folder view state.
    private String cwd = "";
    private String searchQuery = "";  // non-empty → show a flat list of matches
    private List<FileEntry> files = new ArrayList<>();
    private List<String> folders = new ArrayList<>();
    // files keyed by path, rebuilt with each manifest so render() looks rows up
    // in constant time instead of scanning every file per row.
    private Map<String, FileEntry> fileIndex = new HashMap<>();

    // Per-process progress panel: one bar per task type, shown only when the
    // "Tasks" strip is expanded. Each type has an independent bar so concurrent
    // processes (e.g. a transfer and a delete) each show their own.
    private static final int TASK_TRANSFER = 0;
    private static final int TASK_DELETE = 1;
    private static final int TASK_MOVE = 2;
    private static final int TASK_COUNT = 3;
    private final boolean[] taskActive = new boolean[TASK_COUNT];
    private final int[] taskPct = new int[TASK_COUNT];
    private final String[] taskDetail = new String[TASK_COUNT];
    private boolean tasksExpanded = false;
    private View tasksPanel;
    private TextView tasksEmpty;
    private Button tasksBtn;
    private final View[] taskRow = new View[TASK_COUNT];
    private final TextView[] taskLabel = new TextView[TASK_COUNT];
    private final ProgressBar[] taskBar = new ProgressBar[TASK_COUNT];
    private final Button[] taskCancel = new Button[TASK_COUNT];
    private int currentTransferId = -1;  // id of the running transfer, from QUEUE_UPDATE
    // Server paths already queued or downloading, from QUEUE_UPDATE: asking for
    // one of them again does nothing.
    private java.util.Set<String> claimedDownloads = new java.util.HashSet<>();

    // Queue of waiting transfers, mirrored from TransferService's QUEUE_UPDATE broadcasts.
    private boolean queueExpanded = false;
    private View queuePanel;
    private TextView queueEmpty;
    private Button queueBtn;
    private android.widget.LinearLayout queueList;
    private String[] queueLabels = new String[0];
    private int[] queueIds = new int[0];

    private final ExecutorService io = Executors.newSingleThreadExecutor();
    // In-app operations (delete/move) run here — off the UI thread and separate from
    // `io` (manifest/checks) — so they never lock the UI or block polling. They run
    // one at a time; waiting ones show in the Queue panel.
    private final ExecutorService opsExec = Executors.newSingleThreadExecutor();
    private final java.util.LinkedList<InAppOp> opQueue = new java.util.LinkedList<>();
    private InAppOp runningOp = null;
    private int opIdGen = 0;
    private final Handler ui = new Handler(Looper.getMainLooper());
    // True while the running delete is one that undoes itself when stopped, so the
    // cancel prompt can say so instead of warning that finished work is kept.
    private boolean deleteRollsBack = false;
    private boolean busy = false;
    private boolean polling = false;
    private boolean promptedSettings = false;
    private boolean promptedFolder = false;

    private ActivityResultLauncher<Intent> settingsLauncher;
    private ActivityResultLauncher<Intent> pickLauncher;
    private ActivityResultLauncher<Intent> folderPickLauncher;
    private ActivityResultLauncher<String> notifPermLauncher;

    /** Mirrors the transfer service's progress into the in-app bar while visible. */
    private final BroadcastReceiver progressReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context c, Intent i) {
            if (TransferService.QUEUE_UPDATE.equals(i.getAction())) {
                queueLabels = i.getStringArrayExtra(TransferService.Q_LABELS);
                queueIds = i.getIntArrayExtra(TransferService.Q_IDS);
                if (queueLabels == null) queueLabels = new String[0];
                if (queueIds == null) queueIds = new int[0];
                currentTransferId = i.getIntExtra(TransferService.Q_CURRENT_ID, -1);
                String[] claimed = i.getStringArrayExtra(TransferService.Q_CLAIMED);
                claimedDownloads = new java.util.HashSet<>(claimed == null
                        ? Collections.emptyList() : java.util.Arrays.asList(claimed));
                refreshQueue();
                return;
            }
            if (i.getBooleanExtra(TransferService.P_FINISHED, false)) {
                finishTask(TASK_TRANSFER);
                fetchManifest(false);
                return;
            }
            int pct = i.getIntExtra(TransferService.P_PCT, 0);
            String label = i.getStringExtra(TransferService.P_LABEL);
            String speed = i.getStringExtra(TransferService.P_SPEED);
            updateTask(TASK_TRANSFER, pct, (label == null ? "" : label)
                    + (speed == null ? "" : "  " + speed));
            status.setText((label == null ? "" : label) + "  " + pct + "%"
                    + (speed == null ? "" : "  " + speed));
        }
    };

    private final Runnable pollTick = new Runnable() {
        @Override
        public void run() {
            if (!busy) fetchManifest(false);
            if (polling) ui.postDelayed(this, POLL_MS);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        prefs = new Prefs(this);
        store = new SafStore(this, prefs.downloadTreeUri());
        SafStore.forgetLegacyRecords(this);

        status = findViewById(R.id.status);
        location = findViewById(R.id.location);
        search = findViewById(R.id.search);
        RecyclerView list = findViewById(R.id.file_list);
        deleteBtn = findViewById(R.id.btn_delete);
        addBtn = findViewById(R.id.btn_add);
        refreshBtn = findViewById(R.id.btn_refresh);
        openBtn = findViewById(R.id.btn_open);
        upBtn = findViewById(R.id.btn_up);
        newFolderBtn = findViewById(R.id.btn_new_folder);
        trashBtn = findViewById(R.id.btn_trash);

        // Tasks panel (per-process progress bars, hidden behind the strip button).
        tasksPanel = findViewById(R.id.tasks_panel);
        tasksEmpty = findViewById(R.id.tasks_empty);
        tasksBtn = findViewById(R.id.btn_tasks);
        taskRow[TASK_TRANSFER] = findViewById(R.id.row_transfer);
        taskRow[TASK_DELETE] = findViewById(R.id.row_delete);
        taskRow[TASK_MOVE] = findViewById(R.id.row_move);
        taskLabel[TASK_TRANSFER] = findViewById(R.id.lbl_transfer);
        taskLabel[TASK_DELETE] = findViewById(R.id.lbl_delete);
        taskLabel[TASK_MOVE] = findViewById(R.id.lbl_move);
        taskBar[TASK_TRANSFER] = findViewById(R.id.bar_transfer);
        taskBar[TASK_DELETE] = findViewById(R.id.bar_delete);
        taskBar[TASK_MOVE] = findViewById(R.id.bar_move);
        taskCancel[TASK_TRANSFER] = findViewById(R.id.cancel_transfer);
        taskCancel[TASK_DELETE] = findViewById(R.id.cancel_delete);
        taskCancel[TASK_MOVE] = findViewById(R.id.cancel_move);
        taskCancel[TASK_TRANSFER].setOnClickListener(v -> confirmCancelTask(TASK_TRANSFER));
        taskCancel[TASK_DELETE].setOnClickListener(v -> confirmCancelTask(TASK_DELETE));
        taskCancel[TASK_MOVE].setOnClickListener(v -> confirmCancelTask(TASK_MOVE));
        tasksBtn.setOnClickListener(v -> {
            tasksExpanded = !tasksExpanded;
            refreshTasks();
        });
        refreshTasks();

        // Queue strip + panel of waiting transfers.
        queuePanel = findViewById(R.id.queue_panel);
        queueEmpty = findViewById(R.id.queue_empty);
        queueBtn = findViewById(R.id.btn_queue);
        queueList = findViewById(R.id.queue_list);
        queueBtn.setOnClickListener(v -> {
            queueExpanded = !queueExpanded;
            refreshQueue();
        });
        refreshQueue();

        adapter = new FileAdapter(this);
        list.setLayoutManager(new LinearLayoutManager(this));
        list.addItemDecoration(new DividerItemDecoration(this, DividerItemDecoration.VERTICAL));
        list.setAdapter(adapter);

        refreshBtn.setOnClickListener(v -> {
            if (!busy) fetchManifest(true);
        });
        addBtn.setOnClickListener(v -> { if (inTrash()) restoreSelected(); else pickFiles(); });
        deleteBtn.setOnClickListener(v -> { if (inTrash()) deleteForeverSelected(); else deleteSelected(); });
        openBtn.setOnClickListener(v -> { if (inTrash()) confirmEmptyTrash(); else openFolder(); });
        upBtn.setOnClickListener(v -> goUp());
        newFolderBtn.setOnClickListener(v -> promptNewFolder());
        trashBtn.setOnClickListener(v -> toggleTrash());
        trashBtn.setOnDragListener(trashDropListener());

        // Live search: filter to matching files as the user types.
        search.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void onTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void afterTextChanged(Editable e) {
                searchQuery = e.toString().trim();
                render();
            }
        });

        settingsLauncher = registerForActivityResult(
                new ActivityResultContracts.StartActivityForResult(),
                result -> reloadConfig());
        pickLauncher = registerForActivityResult(
                new ActivityResultContracts.StartActivityForResult(),
                result -> {
                    if (result.getResultCode() == RESULT_OK && result.getData() != null) {
                        onFilesPicked(result.getData());
                    }
                });
        folderPickLauncher = registerForActivityResult(
                new ActivityResultContracts.StartActivityForResult(),
                result -> {
                    if (result.getResultCode() == RESULT_OK && result.getData() != null
                            && result.getData().getData() != null) {
                        onFolderPicked(result.getData().getData());
                    }
                });
        notifPermLauncher = registerForActivityResult(
                new ActivityResultContracts.RequestPermission(), granted -> { });

        // Ask for notification permission so the transfer progress notice shows
        // (Android 13+); transfers still run without it, just silently.
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            notifPermLauncher.launch(Manifest.permission.POST_NOTIFICATIONS);
        }

        reloadConfig();
    }

    @Override
    protected void onResume() {
        super.onResume();
        IntentFilter filter = new IntentFilter();
        filter.addAction(TransferService.PROGRESS);
        filter.addAction(TransferService.QUEUE_UPDATE);
        ContextCompat.registerReceiver(this, progressReceiver, filter,
                ContextCompat.RECEIVER_NOT_EXPORTED);
        // Re-sync the queue display in case transfers advanced while we were away.
        try {
            startService(new Intent(this, TransferService.class)
                    .setAction(TransferService.ACTION_QUERY_QUEUE));
        } catch (Exception ignore) {
            // starting a service can be restricted; the live broadcast still keeps us current
        }
        if (prefs.isConfigured()) startPolling();
    }

    @Override
    protected void onPause() {
        super.onPause();
        try {
            unregisterReceiver(progressReceiver);
        } catch (Exception ignore) {
        }
        stopPolling();
    }

    @Override
    public void onBackPressed() {
        // The Back button walks up the folder tree (and out of Trash) before
        // leaving the screen.
        if (inTrash() && cwd.equals(TRASH) && !busy) {
            toggleTrash();
        } else if (!cwd.isEmpty() && !busy) {
            goUp();
        } else {
            super.onBackPressed();
        }
    }

    // -- config --

    private void reloadConfig() {
        if (!prefs.isConfigured()) {
            status.setText(R.string.open_settings_hint);
            if (!promptedSettings) {  // avoid reopening Settings on every back-press
                promptedSettings = true;
                openSettings();
            }
            return;
        }
        promptedSettings = false;
        api = new StorageApi(prefs.serverUrl(), prefs.apiKey());
        cwd = "";
        files = new ArrayList<>();
        folders = new ArrayList<>();
        fileIndex = new HashMap<>();
        adapter.setRows(new ArrayList<>(), "", false);
        render();
        fetchManifest(true);
        startPolling();
        // Prompt once for a download folder if none is chosen yet.
        if (!store.isReady() && !promptedFolder) {
            promptedFolder = true;
            promptFolder();
        }
    }

    private void openSettings() {
        settingsLauncher.launch(new Intent(this, SettingsActivity.class));
    }

    // -- download folder (SAF) --

    /** True if a folder is ready; otherwise prompt the user to pick one. */
    private boolean ensureFolder() {
        if (store.isReady()) return true;
        promptFolder();
        return false;
    }

    private void promptFolder() {
        new AlertDialog.Builder(this)
                .setTitle(R.string.choose_folder_title)
                .setMessage(R.string.choose_folder_msg)
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(R.string.choose, (d, w) -> launchFolderPicker())
                .show();
    }

    private void launchFolderPicker() {
        try {
            folderPickLauncher.launch(new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE));
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, R.string.no_file_picker, Toast.LENGTH_LONG).show();
        }
    }

    private void onFolderPicked(Uri uri) {
        int flags = Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION;
        try {
            getContentResolver().takePersistableUriPermission(uri, flags);
        } catch (Exception ignore) {
            // Some providers grant without persistable access; carry on regardless.
        }
        prefs.saveDownloadTreeUri(uri.toString());
        store = new SafStore(this, uri.toString());
        status.setText(getString(R.string.folder_set, store.folderName()));
    }

    // -- live polling --

    private void startPolling() {
        if (polling || !prefs.isConfigured()) return;
        polling = true;
        ui.postDelayed(pollTick, POLL_MS);
    }

    private void stopPolling() {
        polling = false;
        ui.removeCallbacks(pollTick);
    }

    private void fetchManifest(boolean showLoading) {
        if (api == null) return;
        if (showLoading) status.setText(R.string.loading);
        io.execute(() -> {
            StorageApi.ManifestData data;
            try {
                data = api.fetchManifest();
            } catch (Exception ex) {
                if (showLoading) {
                    // A rejected key isn't a connection problem: say which it is.
                    boolean rejected = ex instanceof StorageApi.HttpError
                            && ((StorageApi.HttpError) ex).code == 401;
                    ui.post(() -> status.setText(rejected
                            ? getString(R.string.key_rejected)
                            : getString(R.string.server_unreachable, ex.getMessage())));
                }
                return;
            }
            List<FileEntry> live = new ArrayList<>();
            for (FileEntry e : data.files) {
                if (!e.deleted) live.add(e);
            }
            List<String> fold = data.folders;
            ui.post(() -> applyManifest(live, fold));
        });
    }

    private void applyManifest(List<FileEntry> newFiles, List<String> newFolders) {
        files = newFiles;
        folders = newFolders;
        fileIndex = new HashMap<>(newFiles.size() * 2);
        for (FileEntry e : newFiles) fileIndex.put(e.path, e);
        normalizeCwd();
        render();
        if (!busy) {
            if (inTrash()) {
                status.setText(R.string.in_trash_status);
            } else {
                String where = cwd.isEmpty() ? "/" : "/" + cwd;
                status.setText(getString(R.string.n_files_live_here, files.size(), where));
            }
        }
    }

    // -- folder navigation & rendering --

    private List<String> filePaths() {
        List<String> out = new ArrayList<>(files.size());
        for (FileEntry e : files) out.add(e.path);
        return out;
    }

    private FileEntry fileByPath(String path) {
        return fileIndex.get(path);
    }

    /** If the current folder vanished, climb to the nearest existing one. */
    private void normalizeCwd() {
        while (!cwd.isEmpty() && !FolderNav.exists(cwd, filePaths(), folders)) {
            cwd = FolderNav.parent(cwd);
        }
    }

    private void render() {
        List<FileAdapter.Row> rows = new ArrayList<>();

        if (!searchQuery.isEmpty()) {
            // Flat list of every file whose path contains the query (any folder).
            // Outside Trash, trashed files don't show; inside it, only they do.
            String q = searchQuery.toLowerCase(Locale.US);
            boolean trash = inTrash();
            List<String> matches = new ArrayList<>();
            for (FileEntry e : files) {
                if (e.path.toLowerCase(Locale.US).contains(q)
                        && Plans.inTrash(e.path) == trash) {
                    matches.add(e.path);
                }
            }
            Collections.sort(matches);
            for (String path : matches) {
                FileEntry e = fileByPath(path);
                if (e != null) rows.add(FileAdapter.Row.file(e));
            }
            adapter.setRows(rows, cwd, true);
            location.setText(getString(R.string.search_matches, searchQuery, matches.size()));
            upBtn.setEnabled(false);
            updateTrashButton();
            return;
        }

        // At the top of Trash there's nowhere to go "up" to: Back leaves Trash.
        boolean canGoUp = !cwd.isEmpty() && !cwd.equals(TRASH);
        if (canGoUp) rows.add(FileAdapter.Row.up());
        FolderNav.Contents contents = FolderNav.contents(cwd, filePaths(), folders);
        for (String folder : contents.folders) {
            if (cwd.isEmpty() && folder.equals(TRASH)) continue;  // it has its own button
            rows.add(FileAdapter.Row.dir(folder));
        }
        for (String path : contents.files) {
            FileEntry e = fileByPath(path);
            if (e != null) rows.add(FileAdapter.Row.file(e));
        }
        adapter.setRows(rows, cwd, false);
        location.setText(inTrash() ? cwd.replace("/", " › ") : (cwd.isEmpty() ? "/" : "/" + cwd));
        upBtn.setEnabled(canGoUp && !busy);
        updateBottomBarForContext();
        updateTrashButton();
    }

    // -- the Trash button --

    /** Open Trash, or go back to the folder the user was in before. */
    private void toggleTrash() {
        if (busy) return;
        if (!searchQuery.isEmpty()) search.setText("");
        adapter.clearSelection();
        if (inTrash()) {
            String back = cwdBeforeTrash;
            navigate(FolderNav.exists(back, filePaths(), folders) ? back : "");
        } else {
            cwdBeforeTrash = cwd;
            navigate(TRASH);
        }
    }

    /** "Trash (n)" normally, "Back to files" while inside Trash. */
    private void updateTrashButton() {
        if (inTrash()) {
            trashBtn.setText(R.string.back_to_files);
            return;
        }
        int n = 0;
        for (FileEntry e : files) if (e.path.startsWith(TRASH + "/")) n++;
        trashBtn.setText(n > 0 ? getString(R.string.trash_n, n) : getString(R.string.trash));
    }

    /** Dropping dragged rows on the Trash button moves them to Trash. */
    private View.OnDragListener trashDropListener() {
        return (v, event) -> {
            switch (event.getAction()) {
                case android.view.DragEvent.ACTION_DRAG_STARTED:
                    Object s = event.getLocalState();
                    return !inTrash() && (s instanceof FileEntry || s instanceof FileAdapter.FolderRef);
                case android.view.DragEvent.ACTION_DRAG_ENTERED:
                    v.setAlpha(0.5f);
                    return true;
                case android.view.DragEvent.ACTION_DRAG_EXITED:
                case android.view.DragEvent.ACTION_DRAG_ENDED:
                    v.setAlpha(1f);
                    return true;
                case android.view.DragEvent.ACTION_DROP:
                    v.setAlpha(1f);
                    onDropOnTrash(event.getLocalState());
                    return true;
                default:
                    return true;
            }
        };
    }

    private void onDropOnTrash(Object dragged) {
        if (busy) return;
        String path = dragged instanceof FileEntry ? ((FileEntry) dragged).path
                : dragged instanceof FileAdapter.FolderRef ? ((FileAdapter.FolderRef) dragged).path
                : null;
        if (path == null) return;
        List<String> selFiles = new ArrayList<>();
        for (FileEntry e : adapter.selectedEntries()) selFiles.add(e.path);
        List<String> selFolders = adapter.selectedFolderPaths();
        // Dropping something that's part of a multi-selection trashes the whole selection.
        if (adapter.selectedCount() > 1 && (selFiles.contains(path) || selFolders.contains(path))) {
            trashItems(selFiles, selFolders);
        } else if (dragged instanceof FileEntry) {
            trashItems(Collections.singletonList(path), Collections.emptyList());
        } else {
            trashItems(Collections.emptyList(), Collections.singletonList(path));
        }
    }

    // -- per-process task progress panel --

    /** Mark a task type active at {@code pct} with optional detail text; refresh the panel. */
    private void updateTask(int type, int pct, String detail) {
        taskActive[type] = true;
        taskPct[type] = pct;
        taskDetail[type] = detail;
        refreshTasks();
    }

    /** Mark a task type finished and hide its bar. */
    private void finishTask(int type) {
        taskActive[type] = false;
        taskPct[type] = 0;
        taskDetail[type] = null;
        refreshTasks();
    }

    /** Post throttled per-file progress for a task type from a background loop. */
    private void postTaskProgress(int type, int[] lastPct, int done, int total) {
        postTaskProgress(type, lastPct, done, total, null);
    }

    /** Same, with a detail string shown next to the percentage. */
    private void postTaskProgress(int type, int[] lastPct, int done, int total, String detail) {
        int pct = total > 0 ? (int) (done * 100L / total) : 100;
        if (pct != lastPct[0]) {
            lastPct[0] = pct;
            final int p = pct;
            ui.post(() -> updateTask(type, p, detail));
        }
    }

    // Compatibility shims so the delete/move call sites stay tidy.
    private void startDeleteBar() { updateTask(TASK_DELETE, 0, null); }
    private void endDeleteBar()   { finishTask(TASK_DELETE); }
    private void startMoveBar()   { updateTask(TASK_MOVE, 0, null); }
    private void endMoveBar()     { finishTask(TASK_MOVE); }
    private void postDeleteProgress(int[] lastPct, int done, int total) {
        postTaskProgress(TASK_DELETE, lastPct, done, total);
    }
    private void postMoveProgress(int[] lastPct, int done, int total) {
        postTaskProgress(TASK_MOVE, lastPct, done, total);
    }
    // A rollback rides the move bar, labelled so it reads as an undo, not a move.
    private void startRestoreBar() { updateTask(TASK_MOVE, 0, getString(R.string.restoring)); }
    private void postRestoreProgress(int[] lastPct, int done, int total) {
        postTaskProgress(TASK_MOVE, lastPct, done, total, getString(R.string.restoring));
    }

    /** Refresh the task rows, the strip button's active-count, and the panel visibility. */
    private void refreshTasks() {
        int[] names = {R.string.task_transfer, R.string.task_delete, R.string.task_move};
        int active = 0;
        for (int t = 0; t < TASK_COUNT; t++) {
            if (taskActive[t]) {
                active++;
                taskRow[t].setVisibility(View.VISIBLE);
                taskBar[t].setProgress(taskPct[t]);
                String d = taskDetail[t];
                taskLabel[t].setText(getString(names[t]) + "  " + taskPct[t] + "%"
                        + (d == null || d.isEmpty() ? "" : "  " + d));
            } else {
                taskRow[t].setVisibility(View.GONE);
            }
        }
        tasksBtn.setText(active > 0 ? getString(R.string.tasks_active, active)
                : getString(R.string.tasks_idle));
        // Highlight the strip while work is running so it's noticeable when collapsed.
        tasksBtn.setTextColor(active > 0
                ? ContextCompat.getColor(this, R.color.accent) : 0xFF888888);
        tasksEmpty.setVisibility(active == 0 ? View.VISIBLE : View.GONE);
        tasksPanel.setVisibility(tasksExpanded ? View.VISIBLE : View.GONE);
    }

    /** A background op body; it must check {@code cancelled} between items and stop when set. */
    private interface OpBody {
        void run(AtomicBoolean cancelled);
    }

    /** One in-app operation (delete/move); its body checks {@code cancelled} between items. */
    private static final class InAppOp {
        final int id;
        final String label;
        final OpBody body;
        final AtomicBoolean cancelled = new AtomicBoolean(false);
        InAppOp(int id, String label, OpBody body) {
            this.id = id;
            this.label = label;
            this.body = body;
        }
    }

    /** Queue an in-app op; it runs (one at a time) off the UI thread when its turn comes. */
    private void enqueueOp(String label, OpBody body) {
        opQueue.add(new InAppOp(++opIdGen, label, body));
        refreshQueue();
        pumpOps();
    }

    /** Flag the running in-app op to stop after its current item (finish current, stop rest). */
    private void cancelRunningOp() {
        if (runningOp != null) {
            runningOp.cancelled.set(true);
            status.setText(R.string.cancelling);
        }
    }

    private void pumpOps() {
        if (runningOp != null || opQueue.isEmpty()) return;
        runningOp = opQueue.removeFirst();
        refreshQueue();  // it left the waiting list (it's now running, shown in Tasks)
        final InAppOp op = runningOp;
        opsExec.execute(() -> {
            try {
                op.body.run(op.cancelled);
            } catch (Exception ignore) {
                // completion posting handles user-facing errors
            } finally {
                ui.post(() -> { runningOp = null; deleteRollsBack = false; pumpOps(); });
            }
        });
    }

    /** Remove a still-waiting in-app op by id (no effect once it's running). */
    private void cancelOp(int id) {
        for (java.util.Iterator<InAppOp> it = opQueue.iterator(); it.hasNext(); ) {
            if (it.next().id == id) { it.remove(); break; }
        }
        refreshQueue();
    }

    /** Rebuild the Queue panel from both waiting transfers and waiting in-app ops. */
    private void refreshQueue() {
        queueList.removeAllViews();
        int total = 0;
        for (int idx = 0; idx < queueLabels.length; idx++) {
            final int id = idx < queueIds.length ? queueIds[idx] : -1;
            addQueueRow(queueLabels[idx], () -> cancelQueued(id));
            total++;
        }
        for (InAppOp op : opQueue) {
            final int id = op.id;
            addQueueRow(op.label, () -> cancelOp(id));
            total++;
        }
        queueBtn.setText(total > 0 ? getString(R.string.queue_active, total)
                : getString(R.string.queue_idle));
        queueBtn.setTextColor(total > 0
                ? ContextCompat.getColor(this, R.color.accent) : 0xFF888888);
        queueEmpty.setVisibility(total == 0 ? View.VISIBLE : View.GONE);
        queuePanel.setVisibility(queueExpanded ? View.VISIBLE : View.GONE);
    }

    private void addQueueRow(String label, Runnable onRemove) {
        View row = getLayoutInflater().inflate(R.layout.item_queue, queueList, false);
        ((TextView) row.findViewById(R.id.q_label)).setText(label);
        row.findViewById(R.id.q_remove).setOnClickListener(v -> onRemove.run());
        queueList.addView(row);
    }

    /** Ask the service to drop a still-waiting transfer by id. */
    private void cancelQueued(int id) {
        if (id < 0) return;
        startService(new Intent(this, TransferService.class)
                .setAction(TransferService.ACTION_CANCEL_QUEUED)
                .putExtra(TransferService.EXTRA_ID, id));
    }

    private void navigate(String folder) {
        cwd = folder;
        render();
        if (inTrash()) {
            status.setText(R.string.in_trash_status);
            return;
        }
        String where = cwd.isEmpty() ? "/ (root)" : "/" + cwd;
        status.setText("Location: " + where);
    }

    private void goUp() {
        if (!cwd.isEmpty() && !cwd.equals(TRASH) && !busy) navigate(FolderNav.parent(cwd));
    }

    @Override
    public void onOpenFolder(String folderPath) {
        if (!busy) navigate(folderPath);
    }

    @Override
    public void onUp() {
        goUp();
    }

    // -- per-row action (download) --

    @Override
    public void onFileAction(FileEntry entry) {
        if (busy) return;
        download(Collections.singletonList(entry.path), Collections.emptyList());
    }

    @Override
    public void onFileSelect(FileEntry entry) {
        onSelectionChanged();
    }

    @Override
    public void onSelectionChanged() {
        // Show how many are selected; Move/Delete act on all of them (files + folders).
        int c = adapter.selectedCount();
        if (c > 0) {
            status.setText(getString(R.string.selected_n, c));
        } else {
            String where = cwd.isEmpty() ? "/" : "/" + cwd;
            status.setText(getString(R.string.n_files_live_here, files.size(), where));
        }
    }

    @Override
    public void onFolderMenu(String folderPath) {
        if (busy) return;
        // Inside Trash, a folder's actions are Open / Restore / Delete forever.
        if (inTrash()) {
            String[] tActions = {
                    getString(R.string.folder_action_open),
                    getString(R.string.folder_action_restore),
                    getString(R.string.folder_action_delete_forever),
            };
            new AlertDialog.Builder(this)
                    .setTitle(FolderNav.leaf(folderPath))
                    .setItems(tActions, (d, w) -> {
                        if (w == 0) navigate(folderPath);
                        else if (w == 1) doRestore(filesUnder(folderPath),
                                subFolderEntries(Collections.singletonList(folderPath)));
                        else confirmDeleteForeverFolder(folderPath);
                    })
                    .show();
            return;
        }
        String[] actions = {
                getString(R.string.folder_action_open),
                getString(R.string.folder_action_download),
                getString(R.string.folder_action_move),
                getString(R.string.folder_action_delete),
        };
        new AlertDialog.Builder(this)
                .setTitle(FolderNav.leaf(folderPath))
                .setItems(actions, (d, w) -> {
                    if (w == 0) navigate(folderPath);
                    else if (w == 1) download(Collections.emptyList(),
                            Collections.singletonList(folderPath));
                    else if (w == 2) promptMoveFolder(folderPath);
                    else confirmDeleteFolder(folderPath);
                })
                .show();
    }

    private void confirmDeleteForeverFolder(String folder) {
        List<FileEntry> under = filesUnder(folder);
        List<String> folderEntries = subFolderEntries(java.util.Collections.singletonList(folder));
        new AlertDialog.Builder(this)
                .setTitle(R.string.delete_forever_title)
                .setMessage(getString(R.string.delete_forever_msg_n, under.size()))
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(R.string.delete_forever, (d, w) -> doDeleteForever(under, folderEntries))
                .show();
    }


    @Override
    public void onFolderDragStarted(String folderPath) {
        status.setText(getString(R.string.drag_hint_folder, FolderNav.leaf(folderPath)));
    }

    @Override
    public void onDropFolderIntoFolder(String srcFolder, String destFolder) {
        if (busy) return;
        String dest = destFolder == null ? "" : destFolder;
        // If the dragged folder is part of a multi-selection, move the whole selection.
        if (adapter.selectedCount() > 1 && adapter.selectedFolderPaths().contains(srcFolder)) {
            doMoveSelection(adapter.selectedEntries(), adapter.selectedFolderPaths(), dest);
            return;
        }
        // Nesting a folder into itself or one of its own descendants is invalid.
        if (dest.equals(srcFolder) || dest.startsWith(srcFolder + "/")) {
            Toast.makeText(this, R.string.folder_move_into_self, Toast.LENGTH_SHORT).show();
            return;
        }
        // Already sitting directly inside the target — nothing to do.
        if (FolderNav.parent(srcFolder).equals(dest)) {
            Toast.makeText(this, getString(R.string.folder_move_noop, FolderNav.leaf(srcFolder)),
                    Toast.LENGTH_SHORT).show();
            return;
        }
        doMoveFolder(srcFolder, dest);
    }

    /**
     * Move a whole selection (files + folders) into {@code dest}. Files keep their
     * name; folders nest under dest keeping their leaf (A → dest/A/…). Folders that
     * can't be nested into the target (itself/descendant, or already there) are
     * skipped.
     */
    private void doMoveSelection(List<FileEntry> selFiles, List<String> selFolders, String destFolder) {
        String raw = destFolder == null ? "" : destFolder.trim();
        while (raw.startsWith("/")) raw = raw.substring(1);
        while (raw.endsWith("/")) raw = raw.substring(0, raw.length() - 1);
        final String dest = raw;
        if (refuseMoveIntoTrash(dest)) return;

        final List<String[]> moves = new ArrayList<>();  // {src, dst}
        final List<String> errors = new ArrayList<>();
        final List<String[]> folderMoves = new ArrayList<>();  // {srcFolder, newFolder}

        // Standalone selected files → dest/filename.
        for (FileEntry e : selFiles) {
            String dst = dest.isEmpty() ? FolderNav.leaf(e.path) : dest + "/" + FolderNav.leaf(e.path);
            if (dst.equals(e.path)) continue;
            if (Plans.isReservedTrashName(dst)) {
                errors.add(e.path + ": " + getString(R.string.trash_reserved));
                continue;
            }
            moves.add(new String[]{e.path, dst});
        }
        // Selected folders → nest under dest, keeping the folder's leaf.
        for (String folder : selFolders) {
            if (dest.equals(folder) || dest.startsWith(folder + "/")) {
                errors.add(folder + ": " + getString(R.string.folder_move_into_self));
                continue;
            }
            if (FolderNav.parent(folder).equals(dest)) continue;  // already there
            if (dest.isEmpty() && Plans.isReservedTrashName(FolderNav.leaf(folder))) {
                errors.add(folder + ": " + getString(R.string.trash_reserved));
                continue;
            }
            int strip = FolderNav.parent(folder).isEmpty() ? 0 : FolderNav.parent(folder).length() + 1;
            for (FileEntry e : files) {
                if (e.path.equals(folder) || e.path.startsWith(folder + "/")) {
                    String relative = e.path.substring(strip);
                    String dst = dest.isEmpty() ? relative : dest + "/" + relative;
                    if (!dst.equals(e.path)) moves.add(new String[]{e.path, dst});
                }
            }
            String leaf = FolderNav.leaf(folder);
            folderMoves.addAll(explicitFolderMoves(folder, dest.isEmpty() ? leaf : dest + "/" + leaf));
        }

        if (moves.isEmpty() && folderMoves.isEmpty() && errors.isEmpty()) return;

        final int total = moves.size();
        enqueueOp(getString(R.string.q_move, Math.max(total, 1)), (cancelled) -> {
            ui.post(this::startMoveBar);
            runMoves(folderMoves, moves, cancelled, errors);
            ui.post(() -> {
                endMoveBar();
                adapter.clearSelection();
                finishOp(cancelled.get(), R.string.move_failed, R.string.moved_n, total, errors);
            });
        });
    }

    /**
     * Things go into Trash only by deleting them, never by a move; and a folder
     * named like Trash in another case ("trash") is reserved too.
     */
    private boolean refuseMoveIntoTrash(String dest) {
        if (Plans.inTrash(dest)) {
            Toast.makeText(this, R.string.use_delete, Toast.LENGTH_LONG).show();
            return true;
        }
        if (Plans.isReservedTrashName(dest)) {
            Toast.makeText(this, R.string.trash_reserved, Toast.LENGTH_LONG).show();
            return true;
        }
        return false;
    }

    /**
     * Say so, and return true, when a top-level item holds Trash's name (see
     * Plans.trashBlocker): the server would refuse every move into Trash.
     */
    private boolean trashBlocked() {
        String name = Plans.trashBlocker(filePaths(), folders);
        if (name == null) return false;
        alert(getString(R.string.delete_failed), getString(R.string.trash_blocked, name));
        return true;
    }

    private void promptMoveFolder(String srcFolder) {
        final List<String> destFolders = new ArrayList<>();
        destFolders.add("");  // root
        for (String f : FolderNav.allFolders(filePaths(), folders)) {
            // Skip the folder itself and its descendants — you can't nest into
            // those — and Trash, which only takes deleted things.
            if (f.equals(srcFolder) || f.startsWith(srcFolder + "/") || Plans.inTrash(f)) continue;
            destFolders.add(f);
        }
        CharSequence[] labels = new CharSequence[destFolders.size()];
        for (int i = 0; i < destFolders.size(); i++) {
            labels[i] = destFolders.get(i).isEmpty()
                    ? getString(R.string.root_folder) : destFolders.get(i);
        }
        new AlertDialog.Builder(this)
                .setTitle(getString(R.string.move_title, FolderNav.leaf(srcFolder)))
                .setItems(labels, (d, w) -> onDropFolderIntoFolder(srcFolder, destFolders.get(w)))
                .setNegativeButton(android.R.string.cancel, null)
                .show();
    }

    /**
     * Every explicit folder at or below {@code folder}, paired with its path once
     * {@code folder} becomes {@code newFolder}. Moving the files doesn't carry
     * these across (an empty one has no files at all), so {@link #runMoves}
     * recreates them. Called on the UI thread, which owns {@code folders}.
     */
    private List<String[]> explicitFolderMoves(String folder, String newFolder) {
        List<String[]> out = new ArrayList<>();
        for (String f : folders) {
            if (f.equals(folder) || f.startsWith(folder + "/")) {
                out.add(new String[]{f, newFolder + f.substring(folder.length())});
            }
        }
        return out;
    }

    /**
     * Carry out a planned move on the op lane: recreate the explicit folders at
     * their new paths, move the files, then delete the old folder entries deepest
     * first — except any whose copy failed, and any that still hold one of those
     * or a file that didn't move (it failed, or Stop skipped it).
     */
    private void runMoves(List<String[]> folderMoves, List<String[]> moves,
                          AtomicBoolean cancelled, List<String> errors) {
        List<String> kept = new ArrayList<>();  // copy failed: keep the original
        for (String[] m : folderMoves) {
            try {
                api.createFolder(m[1]);
            } catch (Exception ex) {
                errors.add(m[0] + " → " + m[1] + ": " + ex.getMessage());
                kept.add(m[0]);
            }
        }
        final int[] lastPct = {-1};
        final int total = moves.size();
        List<String> left = new ArrayList<>();  // files still at their source
        int done = 0;
        for (String[] m : moves) {
            if (cancelled.get()) {  // finish current item, stop the rest
                left.add(m[0]);
                continue;
            }
            if (!m[1].equals(m[0])) {
                try {
                    api.move(m[0], m[1]);
                } catch (Exception ex) {
                    errors.add(m[0] + " → " + m[1] + ": " + ex.getMessage());
                    left.add(m[0]);
                }
            }
            postMoveProgress(lastPct, ++done, total);
        }
        List<String> oldFolders = new ArrayList<>();
        for (String[] m : folderMoves) oldFolders.add(m[0]);
        // A folder sorts before everything inside it, so reversed is deepest first.
        Collections.sort(oldFolders, Collections.reverseOrder());
        List<String> leftovers = new ArrayList<>(left);
        leftovers.addAll(kept);
        for (String f : oldFolders) {
            boolean holdsLeftover = kept.contains(f);
            for (String p : leftovers) {
                if (p.startsWith(f + "/")) {
                    holdsLeftover = true;
                    break;
                }
            }
            if (holdsLeftover) continue;
            try {
                api.deleteFolder(f);
            } catch (Exception ex) {
                errors.add(f + ": " + ex.getMessage());
            }
        }
    }

    /**
     * Nest a whole folder under {@code dest} by moving every file below it, keeping
     * the folder's own name as the top level (folder A dropped on B → B/A/…).
     */
    private void doMoveFolder(String srcFolder, String dest) {
        if (refuseMoveIntoTrash(dest)) return;
        String srcPrefix = srcFolder + "/";
        String srcParent = FolderNav.parent(srcFolder);
        // Bytes to strip so the moved paths keep srcFolder's leaf as their top level.
        final int strip = srcParent.isEmpty() ? 0 : srcParent.length() + 1;

        final List<String[]> moves = new ArrayList<>();  // {src, dst}
        for (FileEntry e : files) {
            if (e.path.equals(srcFolder) || e.path.startsWith(srcPrefix)) {
                String relative = e.path.substring(strip);   // e.g. "A/sub/file"
                String dst = dest.isEmpty() ? relative : dest + "/" + relative;
                moves.add(new String[]{e.path, dst});
            }
        }

        String leaf = FolderNav.leaf(srcFolder);
        final String newFolder = dest.isEmpty() ? leaf : dest + "/" + leaf;
        if (Plans.isReservedTrashName(newFolder)) {
            Toast.makeText(this, R.string.trash_reserved, Toast.LENGTH_LONG).show();
            return;
        }
        // Explicit folders (empty ones included) are recreated under dest.
        final List<String[]> folderMoves = explicitFolderMoves(srcFolder, newFolder);
        if (moves.isEmpty() && folderMoves.isEmpty()) {
            Toast.makeText(this, R.string.folder_empty_download, Toast.LENGTH_SHORT).show();
            return;
        }

        final int total = moves.size();
        enqueueOp(getString(R.string.q_move, Math.max(total, 1)), (cancelled) -> {
            ui.post(this::startMoveBar);
            List<String> errors = new ArrayList<>();
            runMoves(folderMoves, moves, cancelled, errors);
            ui.post(() -> {
                endMoveBar();
                finishOp(cancelled.get(), R.string.move_failed, R.string.moved_n, total, errors);
            });
        });
    }

    @Override
    public void onFileDragStarted(FileEntry file) {
        // Hint shown while the file is being dragged. If the dragged file is part
        // of a multi-selection, the whole selection will move together.
        List<FileEntry> sel = adapter.selectedEntries();
        if (sel.size() > 1 && containsPath(sel, file.path)) {
            status.setText(getString(R.string.drag_hint_n, sel.size()));
        } else {
            status.setText(getString(R.string.drag_hint, FolderNav.leaf(file.path)));
        }
    }

    @Override
    public void onDropIntoFolder(FileEntry file, String folderPath) {
        if (busy) return;
        // Dropping a file that's part of a multi-selection moves the whole selection
        // (files + folders); otherwise just the dragged file.
        if (adapter.selectedCount() > 1 && containsPath(adapter.selectedEntries(), file.path)) {
            doMoveSelection(adapter.selectedEntries(), adapter.selectedFolderPaths(), folderPath);
        } else {
            doMove(Collections.singletonList(file), folderPath);
        }
    }

    private static boolean containsPath(List<FileEntry> list, String path) {
        for (FileEntry e : list) {
            if (e.path.equals(path)) return true;
        }
        return false;
    }

    /** The toolbar menu's Download: every selected file and folder. */
    private void downloadSelected() {
        List<String> selFiles = new ArrayList<>();
        for (FileEntry e : adapter.selectedEntries()) selFiles.add(e.path);
        List<String> selFolders = adapter.selectedFolderPaths();
        if (selFiles.isEmpty() && selFolders.isEmpty()) {
            Toast.makeText(this, R.string.select_to_download, Toast.LENGTH_SHORT).show();
            return;
        }
        download(selFiles, selFolders);
    }

    /**
     * Check the chosen items against the download folder, then hand them to the
     * transfer service. This is the only time the app looks at that folder: a
     * folder whose name is already there is refused, and for files whose name is
     * already there the user picks Overwrite, Keep both or Skip — once, for all
     * of them. Everything else goes ahead. A file lands straight in the download
     * folder; a folder lands in it with its own structure.
     */
    private void download(List<String> selFiles, List<String> selFolders) {
        if (busy || api == null || !ensureFolder()) return;
        if (selFiles.isEmpty() && selFolders.isEmpty()) return;
        final java.util.Map<String, Long> sizes = new java.util.HashMap<>();
        for (FileEntry e : files) sizes.put(e.path, e.size);
        final List<String> folderSnap = new ArrayList<>(folders);
        final java.util.Set<String> claimed = new java.util.HashSet<>(claimedDownloads);
        final SafStore s = store;
        final String what = selFiles.size() + selFolders.size() != 1 ? null
                : selFolders.isEmpty() ? FolderNav.leaf(selFiles.get(0))
                : "/" + FolderNav.leaf(selFolders.get(0));
        status.setText(R.string.checking_folder);
        io.execute(() -> {  // the folder checks are SAF I/O
            Plans.DownloadPlan plan = null;
            String error = null;
            try {
                plan = Plans.planDownloads(selFiles, selFolders, sizes, folderSnap, s::exists);
            } catch (Exception ex) {
                error = ex.getMessage();
            }
            final Plans.DownloadPlan p = plan;
            final String err = error;
            ui.post(() -> {
                if (p == null) {
                    alert(getString(R.string.download_failed), err);
                    return;
                }
                onDownloadPlanned(p, claimed, s, what);
            });
        });
    }

    private void onDownloadPlanned(Plans.DownloadPlan plan, java.util.Set<String> claimed,
                                   SafStore s, String what) {
        // Asking for a file that's already queued or downloading does nothing.
        int already = 0;
        for (java.util.Iterator<Plans.Download> it = plan.items.iterator(); it.hasNext(); ) {
            if (claimed.contains(it.next().path)) { it.remove(); already++; }
        }
        for (java.util.Iterator<Plans.Download> it = plan.fileClashes.iterator(); it.hasNext(); ) {
            if (claimed.contains(it.next().path)) { it.remove(); already++; }
        }
        final int alreadyQueued = already;
        if (plan.folderClashes.isEmpty()) {
            askFileClashes(plan, alreadyQueued, s, what);
            return;
        }
        StringBuilder sb = new StringBuilder();
        for (String f : plan.folderClashes) {
            sb.append("• ").append(getString(R.string.folder_exists_line, FolderNav.leaf(f)))
                    .append("\n");
        }
        sb.append("\n").append(getString(R.string.folder_exists_msg, s.folderName()));
        new AlertDialog.Builder(this)
                .setTitle(R.string.folder_exists_title)
                .setMessage(sb.toString())
                .setPositiveButton(android.R.string.ok, null)
                .setOnDismissListener(dlg -> askFileClashes(plan, alreadyQueued, s, what))
                .show();
    }

    /** One question for every file whose name is already taken in the download folder. */
    private void askFileClashes(Plans.DownloadPlan plan, int already, SafStore s, String what) {
        int n = plan.fileClashes.size();
        if (n == 0) {
            queueDownload(plan, already, what);
            return;
        }
        StringBuilder names = new StringBuilder();
        for (int i = 0; i < n && i < 12; i++) {
            names.append("• ").append(FolderNav.leaf(plan.fileClashes.get(i).dest)).append("\n");
        }
        if (n > 12) names.append("…and ").append(n - 12).append(" more\n");
        String head = n == 1
                ? getString(R.string.file_exists_one, s.folderName())
                : getString(R.string.file_exists_n, n, s.folderName());
        String question = getString(n == 1 ? R.string.file_exists_ask_one : R.string.file_exists_ask_n);
        new AlertDialog.Builder(this)
                .setTitle(R.string.file_exists_title)
                .setMessage(head + "\n\n" + names + "\n" + question)
                .setPositiveButton(R.string.overwrite,
                        (dlg, w) -> resolveAndQueue(plan, Plans.OVERWRITE, already, s, what))
                .setNeutralButton(R.string.keep_both,
                        (dlg, w) -> resolveAndQueue(plan, Plans.KEEP_BOTH, already, s, what))
                .setNegativeButton(R.string.skip,
                        (dlg, w) -> resolveAndQueue(plan, Plans.SKIP, already, s, what))
                .setOnCancelListener(dlg -> status.setText(R.string.download_cancelled))
                .show();
    }

    private void resolveAndQueue(Plans.DownloadPlan plan, String choice, int already,
                                 SafStore s, String what) {
        io.execute(() -> {  // "Keep both" looks for a free name: SAF I/O
            Plans.resolveClashes(plan, choice, s::exists);
            ui.post(() -> queueDownload(plan, already, what));
        });
    }

    /** Hand a planned download to the foreground service so it survives backgrounding. */
    private void queueDownload(Plans.DownloadPlan plan, int already, String what) {
        int n = plan.items.size();
        if (n == 0 && plan.dirs.isEmpty()) {
            status.setText(already > 0 ? getString(R.string.already_queued)
                    : getString(R.string.nothing_downloaded));
            return;
        }
        String[] paths = new String[n];
        String[] dests = new String[n];
        boolean[] overwrite = new boolean[n];
        long[] sizes = new long[n];
        for (int k = 0; k < n; k++) {
            Plans.Download dl = plan.items.get(k);
            paths[k] = dl.path;
            dests[k] = dl.dest;
            overwrite[k] = dl.overwrite;
            sizes[k] = dl.size;
        }
        String label = what != null ? getString(R.string.q_download, what)
                : getString(R.string.q_download_n, n);
        Intent i = new Intent(this, TransferService.class)
                .setAction(TransferService.ACTION_DOWNLOAD)
                .putExtra(TransferService.EXTRA_PATHS, paths)
                .putExtra(TransferService.EXTRA_DESTS, dests)
                .putExtra(TransferService.EXTRA_OVERWRITE, overwrite)
                .putExtra(TransferService.EXTRA_SIZES, sizes)
                .putExtra(TransferService.EXTRA_DIRS, plan.dirs.toArray(new String[0]))
                .putExtra(TransferService.EXTRA_LABEL, label);
        ContextCompat.startForegroundService(this, i);
        adapter.clearSelection();
        status.setText(already > 0
                ? getString(R.string.download_queued_n_already, n, already)
                : getString(R.string.download_queued_n, n));
    }

    // -- create folder --

    private void promptNewFolder() {
        if (busy) return;
        final EditText input = new EditText(this);
        input.setHint(R.string.new_folder_hint);
        String where = cwd.isEmpty() ? "/" : "/" + cwd;
        new AlertDialog.Builder(this)
                .setTitle(R.string.new_folder_title)
                .setMessage(getString(R.string.new_folder_in, where))
                .setView(input)
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(R.string.create_action, (d, w) -> {
                    String name = input.getText().toString().trim();
                    if (name.isEmpty()) {
                        Toast.makeText(this, R.string.folder_name_empty, Toast.LENGTH_SHORT).show();
                        return;
                    }
                    String rel;
                    try {
                        rel = SafStore.normalize(cwd.isEmpty() ? name : cwd + "/" + name);
                    } catch (Exception ex) {
                        alert(getString(R.string.could_not_create_folder), ex.getMessage());
                        return;
                    }
                    if (Plans.isReservedTrashName(rel)) {
                        alert(getString(R.string.could_not_create_folder), getString(R.string.trash_reserved));
                        return;
                    }
                    createFolder(rel);
                })
                .show();
    }

    private void createFolder(String rel) {
        setBusy(true, getString(R.string.creating_folder, rel));
        io.execute(() -> {
            String error = null;
            try {
                api.createFolder(rel);
            } catch (Exception ex) {
                error = ex.getMessage();
            }
            String err = error;
            ui.post(() -> {
                setBusy(false, null);
                if (err != null) {
                    alert(getString(R.string.could_not_create_folder), err);
                } else {
                    cwd = rel;  // step into the folder just created
                    status.setText(getString(R.string.created_folder, rel));
                }
                fetchManifest(false);
            });
        });
    }

    // -- move a selection (files and folders) into a folder --

    private void promptMove(List<FileEntry> entries, List<String> selFolders) {
        final List<String> destFolders = new ArrayList<>();
        destFolders.add("");  // root, shown as (root)
        for (String f : FolderNav.allFolders(filePaths(), folders)) {
            if (Plans.inTrash(f)) continue;  // Trash only takes deleted things
            // A selected folder can't go into itself or one of its own descendants.
            boolean inside = false;
            for (String sel : selFolders) {
                if (f.equals(sel) || f.startsWith(sel + "/")) { inside = true; break; }
            }
            if (!inside) destFolders.add(f);
        }

        CharSequence[] labels = new CharSequence[destFolders.size()];
        for (int i = 0; i < destFolders.size(); i++) {
            labels[i] = destFolders.get(i).isEmpty()
                    ? getString(R.string.root_folder) : destFolders.get(i);
        }

        int count = entries.size() + selFolders.size();
        String title = count != 1
                ? getString(R.string.move_title_n, count)
                : getString(R.string.move_title, FolderNav.leaf(
                        entries.isEmpty() ? selFolders.get(0) : entries.get(0).path));
        new AlertDialog.Builder(this)
                .setTitle(title)
                .setItems(labels, (d, w) -> doMoveSelection(entries, selFolders, destFolders.get(w)))
                .setNeutralButton(R.string.type_path, (d, w) -> promptTypePath(entries, selFolders))
                .setNegativeButton(android.R.string.cancel, null)
                .show();
    }

    private void promptTypePath(List<FileEntry> entries, List<String> selFolders) {
        final EditText input = new EditText(this);
        input.setHint(R.string.type_path_hint);
        input.setText(cwd);
        new AlertDialog.Builder(this)
                .setTitle(R.string.type_path_title)
                .setView(input)
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(R.string.move_action, (d, w) -> {
                    String dest;
                    try {  // the typed path is checked as a picked one would be
                        // A leading "_/" keeps a blank, "/" or "." (the root)
                        // legal; "..", for one, is still refused.
                        String probe = SafStore.normalize("_/" + input.getText().toString().trim());
                        dest = probe.equals("_") ? "" : probe.substring(2);
                    } catch (Exception ex) {
                        alert(getString(R.string.move_failed), ex.getMessage());
                        return;
                    }
                    doMoveSelection(entries, selFolders, dest);
                })
                .show();
    }

    private void doMove(List<FileEntry> entries, String destFolder) {
        String raw = destFolder == null ? "" : destFolder.trim();
        while (raw.startsWith("/")) raw = raw.substring(1);
        while (raw.endsWith("/")) raw = raw.substring(0, raw.length() - 1);
        final String folder = raw;
        if (refuseMoveIntoTrash(folder)) return;
        final int total = entries.size();

        enqueueOp(getString(R.string.q_move, total), (cancelled) -> {
            ui.post(this::startMoveBar);
            List<String> errors = new ArrayList<>();
            final int[] lastPct = {-1};
            int done = 0;
            for (FileEntry e : entries) {
                if (cancelled.get()) break;  // finish current item, stop the rest
                String filename = FolderNav.leaf(e.path);
                String dst = null;
                try {
                    dst = SafStore.normalize(folder.isEmpty() ? filename : folder + "/" + filename);
                } catch (Exception ex) {
                    errors.add(e.path + ": " + ex.getMessage());
                }
                if (dst != null && !dst.equals(e.path) && Plans.isReservedTrashName(dst)) {
                    errors.add(e.path + ": " + getString(R.string.trash_reserved));
                    dst = null;
                }
                if (dst != null && !dst.equals(e.path)) {  // null = normalize failed or refused; equal = already there
                    try {
                        api.move(e.path, dst);
                    } catch (Exception ex) {
                        errors.add(e.path + " → " + dst + ": " + ex.getMessage());
                    }
                }
                postMoveProgress(lastPct, ++done, total);
            }
            ui.post(() -> {
                endMoveBar();
                finishOp(cancelled.get(), R.string.move_failed, R.string.moved_n, total, errors);
            });
        });
    }

    // -- delete: files and folders go to Trash (server only; downloads untouched) --

    private void deleteSelected() {
        if (busy) return;
        List<FileEntry> selFiles = adapter.selectedEntries();
        List<String> selFolders = adapter.selectedFolderPaths();
        if (selFiles.isEmpty() && selFolders.isEmpty()) {
            Toast.makeText(this, R.string.select_first, Toast.LENGTH_SHORT).show();
            return;
        }
        List<String> filePathsSel = new ArrayList<>();
        for (FileEntry e : selFiles) filePathsSel.add(e.path);
        trashItems(filePathsSel, selFolders);
    }

    /** Confirm, then move files and whole folders to Trash. */
    private void trashItems(List<String> selFiles, List<String> selFolders) {
        final List<String> filePathsSel = new ArrayList<>(selFiles);
        final List<String> folderSel = new ArrayList<>(selFolders);
        if (filePathsSel.isEmpty() && folderSel.size() == 1) {
            confirmDeleteFolder(folderSel.get(0));  // keeps its stop-and-undo behaviour
            return;
        }
        java.util.Set<String> all = new java.util.HashSet<>(filePathsSel);
        for (String f : folderSel) {
            for (FileEntry e : filesUnder(f)) all.add(e.path);
        }
        String what = (folderSel.isEmpty() && filePathsSel.size() == 1)
                ? "\'" + filePathsSel.get(0) + "\'"
                : getString(R.string.n_files, all.size());
        confirmTrash(what, () -> doTrash(filePathsSel, folderSel));
    }

    /** The one confirmation before anything goes to Trash. */
    private void confirmTrash(String what, Runnable go) {
        new AlertDialog.Builder(this)
                .setTitle(R.string.trash_confirm_title)
                .setMessage(getString(R.string.trash_confirm_msg, what))
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(R.string.move_to_trash, (d, w) -> go.run())
                .show();
    }

    /**
     * Move files and whole folders to Trash. Where everything lands is worked out
     * now, so nothing already in Trash is ever overwritten (see Plans.planTrash).
     */
    private void doTrash(List<String> selFiles, List<String> selFolders) {
        if (trashBlocked()) return;
        final Plans.TrashPlan plan = Plans.planTrash(selFiles, selFolders, filePaths(), folders);
        // Explicit folders (empty ones included) have no files to carry them
        // across: each is recreated in Trash, so Restore can bring it back.
        final List<String[]> folderMoves = Plans.trashFolderMoves(plan.folderTargets, folders);
        final int total = Math.max(plan.moves.size(), folderMoves.size());
        enqueueOp(getString(R.string.q_trash, total), (cancelled) -> {
            ui.post(this::startDeleteBar);
            List<String> errors = new ArrayList<>();
            final int[] lastPct = {-1};
            int done = 0;
            for (String[] m : plan.moves) {
                if (cancelled.get()) break;  // finish current item, stop the rest
                try {
                    api.move(m[0], m[1]);
                } catch (Exception ex) {
                    errors.add(m[0] + ": " + ex.getMessage());
                }
                postDeleteProgress(lastPct, ++done, plan.moves.size());
            }
            if (!cancelled.get()) carryFolders(folderMoves, errors);
            ui.post(() -> {
                endDeleteBar();
                adapter.clearSelection();
                finishOp(cancelled.get(), R.string.delete_failed, R.string.trashed_n, total, errors);
            });
        });
    }

    /**
     * Recreate explicit folders at their new paths ({old, new} pairs), then drop
     * the originals deepest first. An original whose copy couldn't be made is
     * kept, so no folder is ever lost; one still holding files is refused by the
     * server. Runs on the ops thread.
     */
    private void carryFolders(List<String[]> folderMoves, List<String> errors) {
        java.util.Set<String> kept = new java.util.HashSet<>();
        List<String[]> ordered = new ArrayList<>(folderMoves);
        Collections.sort(ordered, (a, b) -> a[0].compareTo(b[0]));
        for (String[] m : ordered) {
            try {
                api.createFolder(m[1]);
            } catch (Exception ex) {
                errors.add(m[1] + ": " + ex.getMessage());
                kept.add(m[0]);
            }
        }
        // A folder sorts before everything inside it, so reversed is deepest first.
        Collections.reverse(ordered);
        for (String[] m : ordered) {
            if (kept.contains(m[0])) continue;
            try { api.deleteFolder(m[0]); } catch (Exception ignore) {}
        }
    }

    /** Finish a bulk move/delete: status + an error summary if any failed. */
    /** Finish an in-app op: a normal summary, or a "Cancelled" note if it was stopped early. */
    private void finishOp(boolean cancelled, int failTitleRes, int okMsgRes, int total,
                          List<String> errors) {
        if (cancelled) {
            status.setText(R.string.op_cancelled);
            fetchManifest(false);
        } else {
            bulkDone(failTitleRes, okMsgRes, total, errors);
        }
    }

    /** Confirm, then stop the running task of the given type (transfer/delete/move). */
    private void confirmCancelTask(int type) {
        if (!taskActive[type]) return;
        int[] names = {R.string.task_transfer, R.string.task_delete, R.string.task_move};
        String msg = (type == TASK_DELETE && deleteRollsBack)
                ? getString(R.string.cancel_delete_restore_msg)
                : getString(R.string.cancel_task_msg, getString(names[type]));
        new AlertDialog.Builder(this)
                .setTitle(R.string.cancel_task_title)
                .setMessage(msg)
                .setNegativeButton(R.string.keep_going, null)
                .setPositiveButton(R.string.stop, (d, w) -> cancelTask(type))
                .show();
    }

    private void cancelTask(int type) {
        if (type == TASK_TRANSFER) {
            if (currentTransferId >= 0) {
                startService(new Intent(this, TransferService.class)
                        .setAction(TransferService.ACTION_CANCEL_QUEUED)
                        .putExtra(TransferService.EXTRA_ID, currentTransferId));
                status.setText(R.string.cancelling);
            }
        } else {
            cancelRunningOp();  // the single running delete/move op
        }
    }

    private void bulkDone(int failTitleRes, int okMsgRes, int total, List<String> errors) {
        // No setBusy here — delete/move run in the background without locking the UI.
        if (errors.isEmpty()) {
            status.setText(getString(okMsgRes, total));
        } else {
            status.setText(getString(R.string.bulk_partial, total - errors.size(), total));
            alert(getString(failTitleRes), errorList(errors));
        }
        fetchManifest(false);
    }

    /** The first dozen errors as a bulleted list, with a count for the rest. */
    private static String errorList(List<String> errors) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < errors.size() && i < 12; i++) {
            sb.append("• ").append(errors.get(i)).append("\n");
        }
        if (errors.size() > 12) sb.append("…and ").append(errors.size() - 12).append(" more");
        return sb.toString();
    }

    private void confirmDeleteFolder(String folder) {
        List<FileEntry> under = filesUnder(folder);
        if (under.isEmpty()) {
            confirmTrash("/" + folder, () -> doTrash(Collections.emptyList(),
                    Collections.singletonList(folder)));
        } else {
            confirmTrash(getString(R.string.folder_and_n_files, folder, under.size()),
                    () -> doDeleteFolderContents(folder));
        }
    }

    /** Live files at or below {@code folder}. */
    private List<FileEntry> filesUnder(String folder) {
        String prefix = folder + "/";
        List<FileEntry> out = new ArrayList<>();
        for (FileEntry e : files) {
            if (e.path.equals(folder) || e.path.startsWith(prefix)) out.add(e);
        }
        return out;
    }

    /**
     * Move a folder and everything in it to Trash, then drop the emptied shells.
     * Stopping it part-way puts back whatever already reached Trash, so the
     * folder is never left split between Trash and where it was.
     */
    private void doDeleteFolderContents(String folder) {
        if (trashBlocked()) return;
        final Plans.TrashPlan plan = Plans.planTrash(Collections.emptyList(),
                Collections.singletonList(folder), filePaths(), folders);
        final String prefix = folder + "/";
        final List<String[]> folderMoves = Plans.trashFolderMoves(plan.folderTargets, folders);

        final int total = plan.moves.size();
        enqueueOp(getString(R.string.q_delete_folder, folder), (cancelled) -> {
            ui.post(() -> { deleteRollsBack = true; startDeleteBar(); });
            List<String> errors = new ArrayList<>();
            // Moves already made, in order, so a stop can undo them.
            final List<String[]> trashed = new ArrayList<>();
            final int[] lastPct = {-1};
            int done = 0;
            for (String[] m : plan.moves) {
                if (cancelled.get()) break;  // finish current item, stop the rest
                try {
                    api.move(m[0], m[1]);
                    trashed.add(m);
                } catch (Exception ex) {
                    errors.add(m[0] + ": " + ex.getMessage());
                }
                postDeleteProgress(lastPct, ++done, total);
            }
            if (cancelled.get()) {
                // Stopped part-way → undo it. The folder shells are still there
                // (they only go once a delete finishes), so everything lands back
                // exactly where it was.
                restoreAfterCancel(trashed);
                return;
            }
            // Files moved to Trash, so the originals are empty → recreate the
            // explicit folders in Trash and remove them here.
            carryFolders(folderMoves, errors);
            ui.post(() -> {
                deleteRollsBack = false;
                endDeleteBar();
                // The folder is empty now — climb out of it if we're standing inside.
                if (cwd.equals(folder) || cwd.startsWith(prefix)) {
                    cwd = FolderNav.parent(folder);
                }
                bulkDone(R.string.delete_failed, R.string.trashed_n, total, errors);
            });
        });
    }

    /**
     * Roll back the part of a stopped folder delete that already happened: move
     * every file that reached Trash back to its original path, newest first.
     *
     * This is the undo of a cancelled op, so it has no cancel of its own and
     * always runs to the end. Called on the ops thread; it owns the op's
     * completion status, so nothing else posts one afterwards.
     */
    private void restoreAfterCancel(List<String[]> trashed) {
        ui.post(() -> { deleteRollsBack = false; endDeleteBar(); });
        final int total = trashed.size();
        if (total == 0) {  // nothing had been moved yet
            ui.post(() -> {
                status.setText(R.string.op_cancelled);
                fetchManifest(false);
            });
            return;
        }
        ui.post(this::startRestoreBar);
        List<String> errors = new ArrayList<>();
        final int[] lastPct = {-1};
        int done = 0;
        for (int i = total - 1; i >= 0; i--) {  // last deleted, first back
            String[] m = trashed.get(i);
            try {
                api.move(m[1], m[0]);
            } catch (Exception ex) {
                errors.add(m[0] + ": " + ex.getMessage());
            }
            postRestoreProgress(lastPct, ++done, total);
        }
        final int failed = errors.size();
        ui.post(() -> {
            endMoveBar();
            if (failed == 0) {
                status.setText(getString(R.string.cancelled_restored_n, total));
            } else {
                status.setText(getString(R.string.cancelled_restored_partial,
                        total - failed, total));
                alert(getString(R.string.restore_failed), errorList(errors));
            }
            fetchManifest(false);
        });
    }

    private static int depth(String path) {
        int n = 1;
        for (int i = 0; i < path.length(); i++) if (path.charAt(i) == '/') n++;
        return n;
    }

    // -- Trash: restore / delete forever / empty --

    /** True while browsing inside the Trash folder. */
    private boolean inTrash() {
        return cwd.equals(TRASH) || cwd.startsWith(TRASH + "/");
    }

    /** Selected files plus every file under selected folders (deduped, in shown order). */
    private List<FileEntry> selectionFiles() {
        java.util.LinkedHashMap<String, FileEntry> map = new java.util.LinkedHashMap<>();
        for (FileEntry e : adapter.selectedEntries()) map.put(e.path, e);
        for (String folder : adapter.selectedFolderPaths()) {
            for (FileEntry e : filesUnder(folder)) map.put(e.path, e);
        }
        return new ArrayList<>(map.values());
    }

    /** Explicit folder entries at or below the given folders, deepest first. */
    private List<String> subFolderEntries(List<String> selFolders) {
        List<String> out = new ArrayList<>();
        for (String sel : selFolders) {
            String prefix = sel + "/";
            for (String f : folders) {
                if ((f.equals(sel) || f.startsWith(prefix)) && !out.contains(f)) out.add(f);
            }
        }
        Collections.sort(out, (a, b) -> Integer.compare(depth(b), depth(a)));
        return out;
    }

    private void restoreSelected() {
        if (busy) return;
        List<FileEntry> items = selectionFiles();
        List<String> folderEntries = subFolderEntries(adapter.selectedFolderPaths());
        if (items.isEmpty() && folderEntries.isEmpty()) {
            Toast.makeText(this, R.string.select_first, Toast.LENGTH_SHORT).show();
            return;
        }
        doRestore(items, folderEntries);
    }

    /**
     * Move trashed items back to where they came from. One whose spot has been
     * taken since comes back as "name (1).ext" beside it (see Plans.planRestore).
     * Explicit folders in Trash (empty ones included) come back too: each is
     * recreated where it came from and its Trash entry dropped once empty.
     */
    private void doRestore(List<FileEntry> items, List<String> folderEntries) {
        List<String> paths = new ArrayList<>();
        for (FileEntry e : items) paths.add(e.path);
        Map<String, String> movedFrom = new HashMap<>();
        for (FileEntry e : files) movedFrom.put(e.path, e.movedFrom);
        final Map<String, String> origins = Plans.trashOrigins(movedFrom);
        final List<String[]> moves = Plans.planRestore(paths, filePaths(), folders, origins);
        // Folders are spelt as the server and the files coming back spell them.
        List<String> spelt = filePaths();
        for (String[] m : moves) spelt.add(m[1]);
        final List<String[]> folderMoves =
                Plans.restoreFolderMoves(folderEntries, origins, spelt, folders);
        final int total = Math.max(moves.size(), folderMoves.size());
        enqueueOp(getString(R.string.q_restore, total), (cancelled) -> {
            ui.post(this::startMoveBar);
            List<String> errors = new ArrayList<>();
            final int[] lastPct = {-1};
            int done = 0;
            for (String[] m : moves) {
                if (cancelled.get()) break;
                try {
                    api.move(m[0], m[1]);
                } catch (Exception ex) {
                    errors.add(m[0] + " → " + m[1] + ": " + ex.getMessage());
                }
                postMoveProgress(lastPct, ++done, moves.size());
            }
            if (!cancelled.get()) carryFolders(folderMoves, errors);
            ui.post(() -> {
                endMoveBar();
                adapter.clearSelection();
                finishOp(cancelled.get(), R.string.move_failed, R.string.restored_n, total, errors);
            });
        });
    }

    private void deleteForeverSelected() {
        if (busy) return;
        final List<FileEntry> items = selectionFiles();
        final List<String> folderEntries = subFolderEntries(adapter.selectedFolderPaths());
        if (items.isEmpty() && folderEntries.isEmpty()) {
            Toast.makeText(this, R.string.select_first, Toast.LENGTH_SHORT).show();
            return;
        }
        new AlertDialog.Builder(this)
                .setTitle(R.string.delete_forever_title)
                .setMessage(getString(R.string.delete_forever_msg_n, items.size()))
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(R.string.delete_forever, (d, w) -> doDeleteForever(items, folderEntries))
                .show();
    }

    /** Permanently delete items from the server (the irreversible step). */
    private void doDeleteForever(List<FileEntry> items, List<String> folderEntries) {
        final int total = items.size();
        enqueueOp(getString(R.string.q_forever, total), (cancelled) -> {
            ui.post(this::startDeleteBar);
            List<String> errors = new ArrayList<>();
            final int[] lastPct = {-1};
            int done = 0;
            for (FileEntry e : items) {
                if (cancelled.get()) break;
                try {
                    api.delete(e.path);
                } catch (Exception ex) {
                    errors.add(e.path + ": " + ex.getMessage());
                }
                postDeleteProgress(lastPct, ++done, total);
            }
            if (!cancelled.get()) {
                for (String f : folderEntries) {
                    try { api.deleteFolder(f); } catch (Exception ignore) {}
                }
            }
            ui.post(() -> {
                endDeleteBar();
                adapter.clearSelection();
                finishOp(cancelled.get(), R.string.delete_failed, R.string.deleted_n, total, errors);
            });
        });
    }

    private void confirmEmptyTrash() {
        if (busy) return;
        final List<FileEntry> items = new ArrayList<>();
        for (FileEntry e : files) if (e.path.startsWith(TRASH + "/")) items.add(e);
        final List<String> folderEntries = new ArrayList<>();
        for (String f : folders) if (f.equals(TRASH) || f.startsWith(TRASH + "/")) folderEntries.add(f);
        if (items.isEmpty() && folderEntries.isEmpty()) {
            Toast.makeText(this, R.string.trash_is_empty, Toast.LENGTH_SHORT).show();
            return;
        }
        Collections.sort(folderEntries, (a, b) -> Integer.compare(depth(b), depth(a)));
        new AlertDialog.Builder(this)
                .setTitle(R.string.empty_trash_title)
                .setMessage(R.string.empty_trash_msg)
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(R.string.delete_forever, (d, w) -> {
                    if (cwd.startsWith(TRASH)) cwd = "";  // step out; the folder is going away
                    doDeleteForever(items, folderEntries);
                })
                .show();
    }

    /** Swap the bottom-bar actions to Trash mode (Restore / Delete forever / Empty Trash). */
    private void updateBottomBarForContext() {
        if (inTrash()) {
            addBtn.setText(R.string.restore);
            openBtn.setText(R.string.empty_trash);
            newFolderBtn.setEnabled(false);
        } else {
            addBtn.setText(R.string.add_files);
            openBtn.setText(R.string.open_folder);
            newFolderBtn.setEnabled(!busy);
        }
    }

    // -- upload via the document picker (into the current folder) --

    private void pickFiles() {
        if (busy) return;
        if (inTrash()) {
            alert(getString(R.string.cant_upload_title), getString(R.string.cant_upload_msg));
            return;
        }
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType("*/*");
        intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
        try {
            pickLauncher.launch(intent);
        } catch (ActivityNotFoundException e) {
            Toast.makeText(this, R.string.no_file_picker, Toast.LENGTH_LONG).show();
        }
    }

    private void onFilesPicked(Intent data) {
        List<Uri> uris = new ArrayList<>();
        if (data.getClipData() != null) {
            for (int i = 0; i < data.getClipData().getItemCount(); i++) {
                uris.add(data.getClipData().getItemAt(i).getUri());
            }
        } else if (data.getData() != null) {
            uris.add(data.getData());
        }
        if (uris.isEmpty()) return;
        if (inTrash()) {
            alert(getString(R.string.cant_upload_title), getString(R.string.cant_upload_msg));
            return;
        }

        List<String> names = new ArrayList<>();
        for (Uri u : uris) names.add(displayName(u));

        // Uploads never replace anything: a name already used in this folder is
        // refused, and the rest go ahead.
        List<Integer> clashes = Plans.uploadClashes(names, cwd, filePaths(), folders);
        if (clashes.isEmpty()) {
            confirmUpload(uris, names);
            return;
        }
        final List<Uri> okUris = new ArrayList<>();
        final List<String> okNames = new ArrayList<>();
        StringBuilder refused = new StringBuilder();
        int shown = 0;
        for (int k = 0; k < uris.size(); k++) {
            if (clashes.contains(k)) {
                if (shown++ < 12) {
                    refused.append("• ")
                            .append(getString(R.string.file_exists_line, names.get(k)))
                            .append("\n");
                }
            } else {
                okUris.add(uris.get(k));
                okNames.add(names.get(k));
            }
        }
        if (clashes.size() > 12) refused.append("…and ").append(clashes.size() - 12).append(" more\n");
        String where = cwd.isEmpty() ? getString(R.string.root_folder_name) : "/" + cwd;
        refused.append("\n").append(getString(clashes.size() == 1
                ? R.string.not_uploaded_one : R.string.not_uploaded_n, where));
        new AlertDialog.Builder(this)
                .setTitle(R.string.already_exists_title)
                .setMessage(refused.toString())
                .setPositiveButton(android.R.string.ok, null)
                .setOnDismissListener(dlg -> {
                    if (okUris.isEmpty()) status.setText(R.string.nothing_uploaded);
                    else confirmUpload(okUris, okNames);
                })
                .show();
    }

    /** Preview what will be uploaded into the current folder, then send it. */
    private void confirmUpload(List<Uri> uris, List<String> names) {
        StringBuilder preview = new StringBuilder();
        for (int i = 0; i < names.size() && i < 15; i++) {
            preview.append("• ").append(names.get(i)).append("\n");
        }
        if (names.size() > 15) preview.append("…and ").append(names.size() - 15).append(" more");

        String where = cwd.isEmpty() ? getString(R.string.root_folder_name) : "/" + cwd;
        String message = getString(R.string.upload_msg, names.size()) + "\n"
                + getString(R.string.upload_into_folder, where) + "\n\n" + preview;
        new AlertDialog.Builder(this)
                .setTitle(R.string.upload_title)
                .setMessage(message)
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(R.string.upload, (d, w) -> startUpload(uris))
                .show();
    }

    private void startUpload(List<Uri> uris) {
        // Hand the picked files to the foreground service so the upload survives
        // the app going to the background.
        String[] arr = new String[uris.size()];
        for (int k = 0; k < uris.size(); k++) {
            arr[k] = uris.get(k).toString();
        }
        Intent i = new Intent(this, TransferService.class)
                .setAction(TransferService.ACTION_UPLOAD)
                .putExtra(TransferService.EXTRA_URIS, arr)
                .putExtra(TransferService.EXTRA_DEST_DIR, cwd);
        ContextCompat.startForegroundService(this, i);
        status.setText(getString(R.string.upload_queued, uris.size()));
    }

    private String displayName(Uri uri) {
        return TransferService.displayName(this, uri);
    }

    // -- open the save folder (best effort) --

    private void openFolder() {
        if (!store.isReady()) {
            promptFolder();
            return;
        }
        try {
            // Target the picked folder itself (its document URI), not the tree
            // root URI — the latter tends to open the file manager's default view.
            Uri tree = store.treeUri();
            Uri docUri = DocumentsContract.buildDocumentUriUsingTree(
                    tree, DocumentsContract.getTreeDocumentId(tree));
            Intent intent = new Intent(Intent.ACTION_VIEW);
            intent.setDataAndType(docUri, DocumentsContract.Document.MIME_TYPE_DIR);
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                    | Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(intent);
        } catch (Exception e) {
            new AlertDialog.Builder(this)
                    .setTitle(R.string.save_folder_title)
                    .setMessage(getString(R.string.folder_is, store.folderName()))
                    .setPositiveButton(android.R.string.ok, null)
                    .show();
        }
    }

    // -- menu --

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        getMenuInflater().inflate(R.menu.main_menu, menu);
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(@NonNull MenuItem item) {
        int id = item.getItemId();
        if (id == R.id.menu_settings) {
            // Changing the server mid-job would switch the running op (and any
            // queued transfer) to the new server part-way through, so Settings
            // waits until nothing is running or queued — as on the desktop.
            boolean jobsPending = runningOp != null || !opQueue.isEmpty()
                    || currentTransferId >= 0 || queueLabels.length > 0;
            if (jobsPending) {
                Toast.makeText(this, R.string.settings_locked, Toast.LENGTH_LONG).show();
            } else {
                openSettings();
            }
            return true;
        }
        if (id == R.id.menu_download) {
            if (!busy) downloadSelected();
            return true;
        }
        if (id == R.id.menu_move) {
            if (busy) return true;
            // Files and folders alike, as the desktop's Move… does.
            List<FileEntry> sel = adapter.selectedEntries();
            List<String> selFolders = adapter.selectedFolderPaths();
            if (sel.isEmpty() && selFolders.isEmpty()) {
                Toast.makeText(this, R.string.select_file_to_move, Toast.LENGTH_SHORT).show();
            } else {
                promptMove(sel, selFolders);
            }
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    // -- busy / progress --

    private void setBusy(boolean b, String message) {
        busy = b;
        boolean enabled = !b;
        deleteBtn.setEnabled(enabled);
        addBtn.setEnabled(enabled);
        refreshBtn.setEnabled(enabled);
        openBtn.setEnabled(enabled);
        newFolderBtn.setEnabled(enabled);
        upBtn.setEnabled(enabled && !cwd.isEmpty());
        if (message != null) status.setText(message);
    }

    private void alert(String title, String message) {
        new AlertDialog.Builder(this)
                .setTitle(title)
                .setMessage(message)
                .setPositiveButton(android.R.string.ok, null)
                .show();
    }
}
