package com.asyncstorage.dos;

import android.content.Context;
import android.net.Uri;

import androidx.documentfile.provider.DocumentFile;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.List;
import java.util.Locale;

/**
 * The user-chosen download folder, accessed via the Storage Access Framework (a
 * persisted tree URI). The folder is picked once with ACTION_OPEN_DOCUMENT_TREE;
 * it's browsable by the user in the Files app but isn't a public media
 * collection, and needs no storage permissions.
 *
 * Nothing here tracks what was downloaded: once a file is written it's the
 * user's, and the app never looks at it again. The folder is only looked at when
 * a download is asked for, to catch a name that's already taken.
 */
public class SafStore {
    /** SharedPreferences file older versions used to track downloads. */
    private static final String LEGACY_RECORDS = "downloaded";
    private static final int BUF = 1 << 16;

    private final Context ctx;
    private final Uri treeUri;

    public SafStore(Context ctx, String treeUriString) {
        this.ctx = ctx;
        this.treeUri = (treeUriString == null || treeUriString.isEmpty())
                ? null : Uri.parse(treeUriString);
    }

    /** Delete the download records older versions kept; nothing reads them now. */
    public static void forgetLegacyRecords(Context ctx) {
        try {
            ctx.deleteSharedPreferences(LEGACY_RECORDS);
        } catch (Exception ignore) {
            // best effort
        }
    }

    public Uri treeUri() {
        return treeUri;
    }

    private DocumentFile root() {
        return treeUri == null ? null : DocumentFile.fromTreeUri(ctx, treeUri);
    }

    /** A folder is chosen and currently resolvable/writable. */
    public boolean isReady() {
        DocumentFile r = root();
        return r != null && r.canWrite();
    }

    /** The chosen folder's display name, for showing the user. */
    public String folderName() {
        DocumentFile r = root();
        String name = r != null ? r.getName() : null;
        return name != null ? name : "the chosen folder";
    }

    /** Normalise a path to forward-slash form, rejecting unsafe segments. */
    static String normalize(String path) {
        return Plans.normalize(path);
    }

    /**
     * The child of {@code dir} called {@code name}, ignoring case — phone storage
     * treats "Photo.jpg" and "photo.jpg" as the same name.
     */
    private static DocumentFile child(DocumentFile dir, String name) {
        for (DocumentFile f : dir.listFiles()) {
            String n = f.getName();
            if (n != null && n.toLowerCase(Locale.ROOT).equals(name.toLowerCase(Locale.ROOT))) {
                return f;
            }
        }
        return null;
    }

    /** The file or folder at {@code rel} inside the download folder, or null. */
    public DocumentFile find(String rel) {
        DocumentFile dir = root();
        if (dir == null) return null;
        for (String seg : Plans.parts(rel)) {
            if (!dir.isDirectory()) return null;
            dir = child(dir, seg);
            if (dir == null) return null;
        }
        return dir;
    }

    /** True if anything — file or folder — already has this path. */
    public boolean exists(String rel) {
        return find(rel) != null;
    }

    /** The folder at {@code rel}, created (with its parents) if missing. */
    public DocumentFile mkdirs(String rel) {
        DocumentFile dir = root();
        if (dir == null) throw new IllegalStateException("no download folder chosen");
        for (String seg : Plans.parts(rel)) {
            DocumentFile sub = child(dir, seg);
            if (sub == null) sub = dir.createDirectory(seg);
            if (sub == null || !sub.isDirectory()) {
                throw new IllegalStateException("cannot create folder " + seg);
            }
            dir = sub;
        }
        return dir;
    }

    /**
     * Where a download is written while it streams: a fresh "name.part" beside
     * its destination, numbered ("name (1).part") if that name is already taken,
     * so a file of the user's is never replaced. {@link #commit} then puts it in
     * place.
     */
    public DocumentFile beginWrite(String rel) {
        List<String> segs = Plans.parts(rel);
        String parent = FolderNav.parent(Plans.normalize(rel));
        DocumentFile dir = parent.isEmpty() ? root() : mkdirs(parent);
        if (dir == null) throw new IllegalStateException("no download folder chosen");
        final DocumentFile d = dir;
        String tmpName = Plans.freePath(segs.get(segs.size() - 1) + ".part",
                n -> child(d, n) != null, false);
        DocumentFile tmp = dir.createFile("application/octet-stream", tmpName);
        if (tmp == null) throw new IllegalStateException("cannot create file " + tmpName);
        return tmp;
    }

    /**
     * Put a finished download in place as {@code rel}. The name was checked when
     * the user asked; one taken since then (while the job waited in the queue) is
     * never overwritten — the file keeps both instead. Returns the name it got.
     */
    public String commit(DocumentFile tmp, String rel, boolean overwrite) throws IOException {
        String norm = Plans.normalize(rel);
        String parentPath = FolderNav.parent(norm);
        DocumentFile dir = parentPath.isEmpty() ? root() : mkdirs(parentPath);
        if (dir == null) throw new IllegalStateException("no download folder chosen");
        String name = FolderNav.leaf(norm);
        DocumentFile existing = child(dir, name);
        if (existing != null) {
            if (overwrite && existing.isFile()) {
                existing.delete();
            } else {
                final DocumentFile d = dir;
                name = Plans.freePath(name, n -> child(d, n) != null, false);
            }
        }
        if (tmp.renameTo(name)) return name;
        // Some providers can't rename: copy the bytes across instead.
        DocumentFile out = dir.createFile("application/octet-stream", name);
        if (out == null) throw new IOException("cannot create file " + name);
        try (InputStream in = ctx.getContentResolver().openInputStream(tmp.getUri());
             OutputStream os = ctx.getContentResolver().openOutputStream(out.getUri())) {
            if (in == null || os == null) throw new IOException("cannot open " + name);
            byte[] buf = new byte[BUF];
            int n;
            while ((n = in.read(buf)) >= 0) os.write(buf, 0, n);
        } catch (IOException e) {
            out.delete();
            throw e;
        }
        tmp.delete();
        return out.getName() != null ? out.getName() : name;
    }
}
