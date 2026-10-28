package com.asyncstorage.dos;

import org.json.JSONObject;

/** One row of the server manifest: the identity of a single file. */
public class FileEntry {
    public final String path;
    public final String sha256;
    public final long size;
    public final double mtime;
    public final boolean deleted;
    /** Where the file was last moved from ("" if uploaded there); Restore uses it. */
    public final String movedFrom;

    public FileEntry(String path, String sha256, long size, double mtime, boolean deleted,
                     String movedFrom) {
        this.path = path;
        this.sha256 = sha256;
        this.size = size;
        this.mtime = mtime;
        this.deleted = deleted;
        this.movedFrom = movedFrom;
    }

    public static FileEntry fromJson(JSONObject o) {
        return new FileEntry(
                o.optString("path", ""),
                o.optString("sha256", ""),
                o.optLong("size", 0L),
                o.optDouble("mtime", 0.0),
                o.optBoolean("deleted", false),
                o.optString("moved_from", ""));  // older servers omit it
    }
}
