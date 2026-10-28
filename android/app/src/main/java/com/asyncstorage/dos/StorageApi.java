package com.asyncstorage.dos;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.List;

/**
 * Talks to the Async Storage DOS server over HTTP using only the platform
 * HttpURLConnection + org.json (no third-party libraries).
 *
 * Transfers use a read timeout of 0 (infinite) so a slow multi-GB upload or
 * download isn't killed mid-flight; the server keeps its own idle timeout.
 */
public class StorageApi {
    /** Progress across a single transfer. total may be <= 0 when unknown. */
    public interface ProgressListener {
        void onProgress(long done, long total);
    }

    /** Polled during a transfer's byte loop so it can be aborted mid-stream. */
    public interface CancelSignal {
        boolean cancelled();
    }

    /** Thrown when a transfer is aborted via its CancelSignal (distinct from a failure). */
    public static class Cancelled extends IOException {
        public Cancelled() { super("cancelled"); }
    }

    /** A non-2xx response; {@code code} is the HTTP status (-1 if unknown). */
    public static class HttpError extends IOException {
        public final int code;
        public HttpError(int code, String message) {
            super(message);
            this.code = code;
        }
    }

    private static final int CONNECT_TIMEOUT_MS = 15000;
    private static final int BUF = 1 << 16;

    private final String baseUrl;
    private final String apiKey;

    public StorageApi(String baseUrl, String apiKey) {
        this.baseUrl = baseUrl;
        this.apiKey = apiKey;
    }

    private HttpURLConnection open(String method, String urlStr) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(urlStr).openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(CONNECT_TIMEOUT_MS);
        c.setRequestProperty("X-API-Key", apiKey);
        return c;
    }

    private static String enc(String path) throws IOException {
        // The server decodes %2F back to '/' and '+' back to space.
        return URLEncoder.encode(path, "UTF-8");
    }

    private static String readBody(InputStream in) throws IOException {
        if (in == null) return "";
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[BUF];
        int n;
        while ((n = in.read(buf)) >= 0) {
            bos.write(buf, 0, n);
        }
        return bos.toString("UTF-8");
    }

    private static HttpError httpError(HttpURLConnection c) {
        String detail;
        try {
            detail = readBody(c.getErrorStream());
        } catch (IOException e) {
            detail = "";
        }
        // Prefer the server's JSON "detail" message (e.g. "folder is not empty").
        if (!detail.isEmpty()) {
            try {
                detail = new JSONObject(detail).optString("detail", detail);
            } catch (Exception ignore) {
                // not JSON; keep the raw body
            }
        }
        int code;
        try {
            code = c.getResponseCode();
        } catch (IOException e) {
            code = -1;
        }
        if (detail.isEmpty()) return new HttpError(code, "HTTP " + code);
        return new HttpError(code, detail);
    }

    /** A tiny POST with an empty body, raising on a non-2xx response. */
    private void postEmpty(String urlStr) throws Exception {
        HttpURLConnection c = open("POST", urlStr);
        c.setDoOutput(true);
        c.setFixedLengthStreamingMode(0);
        try {
            c.getOutputStream().close();
            if (c.getResponseCode() / 100 != 2) throw httpError(c);
        } finally {
            c.disconnect();
        }
    }

    /** GET /health — returns the file count, or -1 if the key was rejected. */
    public int health() throws Exception {
        HttpURLConnection c = open("GET", baseUrl + "/health");
        try {
            if (c.getResponseCode() / 100 != 2) throw httpError(c);
            JSONObject o = new JSONObject(readBody(c.getInputStream()));
            return o.has("files") ? o.getInt("files") : -1;
        } finally {
            c.disconnect();
        }
    }

    /** Files (live and tombstoned) plus explicitly-created folders. */
    public static class ManifestData {
        public final List<FileEntry> files;
        public final List<String> folders;

        public ManifestData(List<FileEntry> files, List<String> folders) {
            this.files = files;
            this.folders = folders;
        }
    }

    /** GET /manifest — every live-or-tombstoned file the server knows. */
    public List<FileEntry> getManifest() throws Exception {
        return fetchManifest().files;
    }

    /** GET /manifest — files and folders in one request. */
    public ManifestData fetchManifest() throws Exception {
        HttpURLConnection c = open("GET", baseUrl + "/manifest");
        try {
            if (c.getResponseCode() / 100 != 2) throw httpError(c);
            JSONObject o = new JSONObject(readBody(c.getInputStream()));
            JSONArray arr = o.getJSONArray("files");
            List<FileEntry> files = new ArrayList<>(arr.length());
            for (int i = 0; i < arr.length(); i++) {
                files.add(FileEntry.fromJson(arr.getJSONObject(i)));
            }
            List<String> folders = new ArrayList<>();
            JSONArray farr = o.optJSONArray("folders");  // older servers omit this
            if (farr != null) {
                for (int i = 0; i < farr.length(); i++) {
                    String f = farr.optString(i, "");
                    if (!f.isEmpty()) folders.add(f);
                }
            }
            return new ManifestData(files, folders);
        } finally {
            c.disconnect();
        }
    }

    /**
     * GET /download — stream to dest atomically, verifying the sha256 header.
     * Writes to a .part temp then renames; sets the file mtime from X-Mtime.
     */
    public void downloadToFile(String path, File dest, ProgressListener progress) throws Exception {
        HttpURLConnection c = open("GET", baseUrl + "/download?path=" + enc(path));
        c.setReadTimeout(0);
        try {
            if (c.getResponseCode() / 100 != 2) throw httpError(c);
            String sha = c.getHeaderField("X-Content-SHA256");
            double mtime = parseDouble(c.getHeaderField("X-Mtime"), 0);
            long total = c.getContentLengthLong();

            File parent = dest.getParentFile();
            if (parent != null) parent.mkdirs();
            File tmp = new File(dest.getAbsolutePath() + ".part");

            MessageDigest md = MessageDigest.getInstance("SHA-256");
            long received = 0;
            try (InputStream in = new BufferedInputStream(c.getInputStream());
                 OutputStream out = new FileOutputStream(tmp)) {
                byte[] buf = new byte[BUF];
                int n;
                while ((n = in.read(buf)) >= 0) {
                    out.write(buf, 0, n);
                    md.update(buf, 0, n);
                    received += n;
                    if (progress != null) progress.onProgress(received, total);
                }
            } catch (Exception e) {
                tmp.delete();
                throw e;
            }

            if (sha != null && !sha.isEmpty() && !Hashing.hex(md.digest()).equals(sha)) {
                tmp.delete();
                throw new IOException("content hash mismatch");
            }
            if (!tmp.renameTo(dest)) {
                // renameTo can fail across some filesystems; fall back to copy.
                copy(tmp, dest);
                tmp.delete();
            }
            if (mtime > 0) {
                dest.setLastModified((long) (mtime * 1000));
            }
        } finally {
            c.disconnect();
        }
    }

    /**
     * GET /download — stream into the given output stream, verifying the sha256
     * header, and return the verified sha. Used when the destination is a SAF
     * DocumentFile (no java.io.File available). The caller owns/closes the stream
     * and is responsible for deleting the target on failure.
     */
    public String downloadToStream(String path, OutputStream out, ProgressListener progress,
                                   CancelSignal cancel) throws Exception {
        HttpURLConnection c = open("GET", baseUrl + "/download?path=" + enc(path));
        c.setReadTimeout(0);
        try {
            if (c.getResponseCode() / 100 != 2) throw httpError(c);
            String sha = c.getHeaderField("X-Content-SHA256");
            long total = c.getContentLengthLong();

            MessageDigest md = MessageDigest.getInstance("SHA-256");
            long received = 0;
            try (InputStream in = new BufferedInputStream(c.getInputStream())) {
                byte[] buf = new byte[BUF];
                int n;
                while ((n = in.read(buf)) >= 0) {
                    if (cancel != null && cancel.cancelled()) throw new Cancelled();
                    out.write(buf, 0, n);
                    md.update(buf, 0, n);
                    received += n;
                    if (progress != null) progress.onProgress(received, total);
                }
            }
            String got = Hashing.hex(md.digest());
            if (sha != null && !sha.isEmpty() && !got.equals(sha)) {
                throw new IOException("content hash mismatch");
            }
            return got;
        } finally {
            c.disconnect();
        }
    }

    /**
     * PUT /upload — stream a local file to the server with a fixed Content-Length.
     * The sha256 must be computed by the caller (sent in the X-Content-SHA256 header).
     */
    public void uploadFile(String path, File local, String sha, double mtime,
                           ProgressListener progress, CancelSignal cancel) throws Exception {
        long size = local.length();
        HttpURLConnection c = open("PUT", baseUrl + "/upload?path=" + enc(path));
        c.setReadTimeout(0);
        c.setDoOutput(true);
        c.setFixedLengthStreamingMode(size);
        c.setRequestProperty("X-Content-SHA256", sha);
        c.setRequestProperty("X-Mtime", String.valueOf(mtime));
        c.setRequestProperty("Content-Type", "application/octet-stream");
        try {
            long sent = 0;
            try (InputStream in = new BufferedInputStream(new FileInputStream(local));
                 OutputStream out = c.getOutputStream()) {
                byte[] buf = new byte[BUF];
                int n;
                while ((n = in.read(buf)) >= 0) {
                    if (cancel != null && cancel.cancelled()) throw new Cancelled();
                    out.write(buf, 0, n);
                    sent += n;
                    if (progress != null) progress.onProgress(sent, size);
                }
            }
            if (c.getResponseCode() / 100 != 2) throw httpError(c);
        } finally {
            c.disconnect();
        }
    }

    /** POST /delete — tombstone the file on the server. */
    public void delete(String path) throws Exception {
        postEmpty(baseUrl + "/delete?path=" + enc(path));
    }

    /** POST /move — move/rename a file on the server from src to dst. */
    public void move(String src, String dst) throws Exception {
        postEmpty(baseUrl + "/move?src=" + enc(src) + "&dst=" + enc(dst));
    }

    /** POST /folder — create an (empty) folder on the server. */
    public void createFolder(String path) throws Exception {
        postEmpty(baseUrl + "/folder?path=" + enc(path));
    }

    /** POST /folder/delete — delete an empty folder on the server. */
    public void deleteFolder(String path) throws Exception {
        postEmpty(baseUrl + "/folder/delete?path=" + enc(path));
    }

    private static double parseDouble(String s, double dflt) {
        try {
            return s == null ? dflt : Double.parseDouble(s);
        } catch (NumberFormatException e) {
            return dflt;
        }
    }

    private static void copy(File from, File to) throws IOException {
        try (InputStream in = new FileInputStream(from);
             OutputStream out = new FileOutputStream(to)) {
            byte[] buf = new byte[BUF];
            int n;
            while ((n = in.read(buf)) >= 0) {
                out.write(buf, 0, n);
            }
        }
    }
}
