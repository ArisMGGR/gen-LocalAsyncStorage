package com.asyncstorage.dos;

import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.List;
import java.util.TreeSet;

/**
 * Pure folder-tree helpers, mirroring the desktop client's folder_contents.
 * Folders exist either explicitly (created on the server) or implicitly as the
 * prefix of a file's path; these methods derive the tree from both.
 */
public final class FolderNav {
    private FolderNav() {}

    /** Parent folder of a file/folder path ("" for a top-level item). */
    public static String parent(String path) {
        int i = path.lastIndexOf('/');
        return i >= 0 ? path.substring(0, i) : "";
    }

    /** Last path segment (the file or folder name). */
    public static String leaf(String path) {
        int i = path.lastIndexOf('/');
        return i >= 0 ? path.substring(i + 1) : path;
    }

    /** The immediate sub-folders and files directly inside a folder. */
    public static class Contents {
        public final List<String> folders;
        public final List<String> files;

        Contents(List<String> folders, List<String> files) {
            this.folders = folders;
            this.files = files;
        }
    }

    /**
     * Sub-folders and files directly inside {@code cwd} ("" = root). Sub-folders
     * are derived from explicit folders and from file-path prefixes, so a folder
     * shows up whether or not it currently holds files. Full paths, each sorted.
     */
    public static Contents contents(
            String cwd, Collection<String> filePaths, Collection<String> folderPaths) {
        String prefix = cwd.isEmpty() ? "" : cwd + "/";
        int plen = prefix.length();
        TreeSet<String> subs = new TreeSet<>();
        List<String> files = new ArrayList<>();

        for (String p : filePaths) {
            if (!prefix.isEmpty() && !p.startsWith(prefix)) continue;
            String rest = p.substring(plen);
            if (rest.isEmpty()) continue;
            int slash = rest.indexOf('/');
            if (slash >= 0) subs.add(prefix + rest.substring(0, slash));
            else files.add(p);
        }
        for (String f : folderPaths) {
            if (!prefix.isEmpty() && !f.startsWith(prefix)) continue;
            String rest = f.substring(plen);
            if (rest.isEmpty()) continue;  // f == cwd itself
            int slash = rest.indexOf('/');
            subs.add(prefix + (slash >= 0 ? rest.substring(0, slash) : rest));
        }
        Collections.sort(files);
        return new Contents(new ArrayList<>(subs), files);
    }

    /** True if a folder exists — explicit, or implied by a file/sub-folder prefix. */
    public static boolean exists(
            String folder, Collection<String> filePaths, Collection<String> folderPaths) {
        if (folder.isEmpty()) return true;  // the root always exists
        if (folderPaths.contains(folder)) return true;
        String prefix = folder + "/";
        for (String f : folderPaths) if (f.startsWith(prefix)) return true;
        for (String p : filePaths) if (p.startsWith(prefix)) return true;
        return false;
    }

    /** Every folder that exists, explicit or implied by a file path; sorted. */
    public static List<String> allFolders(
            Collection<String> filePaths, Collection<String> folderPaths) {
        TreeSet<String> set = new TreeSet<>(folderPaths);
        for (String p : filePaths) {
            String[] segs = p.split("/");
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < segs.length - 1; i++) {
                if (sb.length() > 0) sb.append('/');
                sb.append(segs[i]);
                set.add(sb.toString());
            }
        }
        return new ArrayList<>(set);
    }
}
