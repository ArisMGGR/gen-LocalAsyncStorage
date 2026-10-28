package com.asyncstorage.dos;

import android.content.ClipData;
import android.graphics.Color;
import android.view.DragEvent;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.TextView;

import androidx.annotation.NonNull;
import androidx.recyclerview.widget.RecyclerView;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * Renders the current folder: an optional ".." row, sub-folders, then files.
 * Tapping a folder opens it; tapping a file selects it (for Download/Move/Delete)
 * and its action cell downloads it.
 *
 * Drag-and-drop: long-press a file to pick it up, then drop it on a folder (or
 * the ".." row) to move it there. Folders highlight while a file hovers over
 * them. Mirrors the desktop client's folder browser.
 */
public class FileAdapter extends RecyclerView.Adapter<FileAdapter.VH> {

    public static final int TYPE_UP = 0;
    public static final int TYPE_DIR = 1;
    public static final int TYPE_FILE = 2;

    /** One rendered line: the ".." row, a sub-folder, or a file. */
    public static class Row {
        final int type;
        final String path;      // folder path, or file path; "" for the ".." row
        final FileEntry file;   // set only for TYPE_FILE

        private Row(int type, String path, FileEntry file) {
            this.type = type;
            this.path = path;
            this.file = file;
        }

        static Row up() { return new Row(TYPE_UP, "", null); }
        static Row dir(String path) { return new Row(TYPE_DIR, path, null); }
        static Row file(FileEntry e) { return new Row(TYPE_FILE, e.path, e); }
    }

    /** Local drag payload marking a folder move (distinct from a file's FileEntry). */
    static final class FolderRef {
        final String path;
        FolderRef(String path) { this.path = path; }
    }

    public interface Listener {
        void onOpenFolder(String folderPath);
        void onUp();
        /** The file's Download action was tapped. */
        void onFileAction(FileEntry entry);
        void onFileSelect(FileEntry entry);
        /** The selection changed (a folder was toggled) — refresh any selection UI. */
        void onSelectionChanged();
        /** The folder's action button was tapped — show its actions menu. */
        void onFolderMenu(String folderPath);
        /** A file was dropped onto a folder (or "..") — move it there. */
        void onDropIntoFolder(FileEntry file, String folderPath);
        /** A folder was dropped onto another folder (or "..") — nest it inside. */
        void onDropFolderIntoFolder(String srcFolder, String destFolder);
        /** A drag of a file began (long-press) — show a hint. */
        void onFileDragStarted(FileEntry file);
        /** A drag of a folder began (long-press) — show a hint. */
        void onFolderDragStarted(String folderPath);
    }

    private static final int SELECTED_BG = Color.parseColor("#334A90E2");
    private static final int DROP_TARGET_BG = Color.parseColor("#5546A0FF");

    private final List<Row> rows = new ArrayList<>();
    private final Listener listener;
    private final Set<String> selectedPaths = new LinkedHashSet<>();
    private final Set<String> selectedFolders = new LinkedHashSet<>();
    private String currentDir = "";
    private boolean flat = false;  // search results: full paths, no drag

    public FileAdapter(Listener listener) {
        this.listener = listener;
    }

    public void setRows(List<Row> newRows, String currentDir, boolean flat) {
        this.currentDir = currentDir == null ? "" : currentDir;
        this.flat = flat;
        rows.clear();
        rows.addAll(newRows);
        // Drop from the selection any files/folders no longer shown.
        List<String> presentFiles = new ArrayList<>();
        List<String> presentFolders = new ArrayList<>();
        for (Row r : newRows) {
            if (r.type == TYPE_FILE) presentFiles.add(r.path);
            else if (r.type == TYPE_DIR) presentFolders.add(r.path);
        }
        selectedPaths.retainAll(presentFiles);
        selectedFolders.retainAll(presentFolders);
        notifyDataSetChanged();
    }

    /** Every currently-selected file, in the order shown. */
    public List<FileEntry> selectedEntries() {
        List<FileEntry> out = new ArrayList<>();
        for (Row r : rows) {
            if (r.type == TYPE_FILE && selectedPaths.contains(r.path)) out.add(r.file);
        }
        return out;
    }

    /** Every currently-selected folder path, in the order shown. */
    public List<String> selectedFolderPaths() {
        List<String> out = new ArrayList<>();
        for (Row r : rows) {
            if (r.type == TYPE_DIR && selectedFolders.contains(r.path)) out.add(r.path);
        }
        return out;
    }

    /** True when at least one file or folder is selected. */
    public boolean hasSelection() {
        return !selectedPaths.isEmpty() || !selectedFolders.isEmpty();
    }

    public int selectedCount() {
        return selectedPaths.size() + selectedFolders.size();
    }

    public void clearSelection() {
        if (!selectedPaths.isEmpty() || !selectedFolders.isEmpty()) {
            selectedPaths.clear();
            selectedFolders.clear();
            notifyDataSetChanged();
        }
    }

    @NonNull
    @Override
    public VH onCreateViewHolder(@NonNull ViewGroup parent, int viewType) {
        View v = LayoutInflater.from(parent.getContext())
                .inflate(R.layout.item_file, parent, false);
        return new VH(v);
    }

    @Override
    public void onBindViewHolder(@NonNull VH h, int position) {
        Row r = rows.get(position);
        // Reset per-row listeners so recycled rows don't keep a stale handler.
        h.itemView.setOnLongClickListener(null);
        h.itemView.setOnDragListener(null);
        h.action.setOnClickListener(null);


        if (r.type == TYPE_UP) {
            h.name.setText("⬆  ..");
            h.size.setText("");
            h.action.setText("");
            h.itemView.setBackgroundColor(Color.TRANSPARENT);
            h.itemView.setOnClickListener(v -> listener.onUp());
            // Dropping a file/folder on ".." moves it up into the parent folder.
            h.itemView.setOnDragListener(dropListener(FolderNav.parent(currentDir)));
            return;
        }

        if (r.type == TYPE_DIR) {
            h.name.setText("📁  " + FolderNav.leaf(r.path));
            h.size.setText("—");
            h.action.setText("⋮");  // the folder's actions menu
            h.itemView.setBackgroundColor(
                    selectedFolders.contains(r.path) ? SELECTED_BG : Color.TRANSPARENT);
            h.action.setOnClickListener(v -> listener.onFolderMenu(r.path));
            // With a selection active, tapping a folder toggles it into the selection
            // instead of opening it (open via the ⋮ menu). With nothing selected, open.
            h.itemView.setOnClickListener(v -> {
                if (hasSelection()) {
                    if (!selectedFolders.remove(r.path)) selectedFolders.add(r.path);
                    notifyDataSetChanged();
                    listener.onSelectionChanged();
                } else {
                    listener.onOpenFolder(r.path);
                }
            });
            // Long-press picks the folder up as a drag; drop it on another folder
            // (or "..") to nest it there. Disabled in flat search results.
            if (!flat) {
                h.itemView.setOnLongClickListener(v -> {
                    ClipData clip = ClipData.newPlainText("folder", r.path);
                    v.startDragAndDrop(clip, new View.DragShadowBuilder(v), new FolderRef(r.path), 0);
                    listener.onFolderDragStarted(r.path);
                    return true;
                });
            }
            h.itemView.setOnDragListener(dropListener(r.path));
            return;
        }

        final FileEntry e = r.file;
        // Search results show the full path; the folder view shows the leaf name.
        h.name.setText(flat ? e.path : "     " + FolderNav.leaf(e.path));
        h.size.setText(Fmt.size(e.size));
        h.action.setText(R.string.download_action);
        h.itemView.setBackgroundColor(
                selectedPaths.contains(e.path) ? SELECTED_BG : Color.TRANSPARENT);
        h.action.setOnClickListener(v -> listener.onFileAction(e));
        // Tap toggles selection; Download/Move/Delete then act on every selected file.
        h.itemView.setOnClickListener(v -> {
            if (!selectedPaths.remove(e.path)) selectedPaths.add(e.path);
            notifyDataSetChanged();
            listener.onFileSelect(e);
        });
        // Long-press picks the file up as a drag; drop it on a folder to move it.
        // Disabled in search results, where no folders are shown to drop onto.
        if (!flat) {
            h.itemView.setOnLongClickListener(v -> {
                ClipData clip = ClipData.newPlainText("path", e.path);
                v.startDragAndDrop(clip, new View.DragShadowBuilder(v), e, 0);
                listener.onFileDragStarted(e);
                return true;
            });
        }
    }

    /** A drop target that moves the dropped file/folder into {@code targetFolder}. */
    private View.OnDragListener dropListener(final String targetFolder) {
        return (v, event) -> {
            switch (event.getAction()) {
                case DragEvent.ACTION_DRAG_STARTED:
                    // Only accept our own file/folder move drags.
                    Object s = event.getLocalState();
                    return s instanceof FileEntry || s instanceof FolderRef;
                case DragEvent.ACTION_DRAG_ENTERED:
                    v.setBackgroundColor(DROP_TARGET_BG);
                    return true;
                case DragEvent.ACTION_DRAG_EXITED:
                case DragEvent.ACTION_DRAG_ENDED:
                    v.setBackgroundColor(Color.TRANSPARENT);
                    return true;
                case DragEvent.ACTION_DROP:
                    v.setBackgroundColor(Color.TRANSPARENT);
                    Object ls = event.getLocalState();
                    if (ls instanceof FileEntry) {
                        listener.onDropIntoFolder((FileEntry) ls, targetFolder);
                    } else if (ls instanceof FolderRef) {
                        listener.onDropFolderIntoFolder(((FolderRef) ls).path, targetFolder);
                    }
                    return true;
                default:
                    return true;  // ACTION_DRAG_LOCATION
            }
        };
    }

    @Override
    public int getItemCount() {
        return rows.size();
    }

    static class VH extends RecyclerView.ViewHolder {
        final TextView name;
        final TextView size;
        final TextView action;

        VH(@NonNull View v) {
            super(v);
            name = v.findViewById(R.id.file_name);
            size = v.findViewById(R.id.file_size);
            action = v.findViewById(R.id.file_action);
        }
    }
}
