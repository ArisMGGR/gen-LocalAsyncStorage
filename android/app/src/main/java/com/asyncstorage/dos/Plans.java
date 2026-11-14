package com.asyncstorage.dos;

import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * Pure planning rules (no Android types), mirroring the desktop client
 * (client/gui.py): where downloads land, which names are already taken, and
 * where deleted/restored items go so nothing is ever overwritten.
 */
public final class Plans {
    private Plans() {}

    /** Top-level folder that deleted items are moved into (so a delete is reversible). */
    public static final String TRASH = "Trash";

    public static final String OVERWRITE = "overwrite";
    public static final String KEEP_BOTH = "keep";
    public static final String SKIP = "skip";

    // -- paths ------------------------------------------------------------------

    static List<String> parts(String path) {
        List<String> keep = new ArrayList<>();
        for (String seg : path.replace('\\', '/').split("/")) {
            if (seg.isEmpty() || seg.equals(".")) continue;
            if (seg.equals("..") || seg.indexOf('\0') >= 0) {
                throw new IllegalArgumentException("unsafe path: " + path);
            }
            keep.add(seg);
        }
        if (keep.isEmpty()) {
            throw new IllegalArgumentException("empty path");
        }
        return keep;
    }

    /** Normalise a path to forward-slash form, rejecting unsafe segments. */
    public static String normalize(String path) {
        return String.join("/", parts(path));
    }

    public static boolean inTrash(String path) {
        return path.equals(TRASH) || path.startsWith(TRASH + "/");
    }

    public static String trashPath(String path) {
        return TRASH + "/" + path;
    }

    /** Where a trashed path came from (strip the leading Trash/). */
    public static String untrash(String path) {
        return path.startsWith(TRASH + "/") ? path.substring(TRASH.length() + 1) : path;
    }

    /**
     * True if {@code path} starts with a top-level name that is "Trash" ignoring
     * case. The server compares names ignoring case, so a top-level "trash"
     * would take Trash's name (see {@link #trashBlocker}).
     */
    public static boolean isReservedTrashName(String path) {
        int slash = path.indexOf('/');
        String top = slash >= 0 ? path.substring(0, slash) : path;
        return lower(top).equals(lower(TRASH));
    }

    /**
     * A top-level item that stops anything reaching Trash, or null: a file named
     * like Trash, or a folder spelt differently from it ("trash") while no
     * "Trash" exists. The server then refuses every move into Trash.
     */
    public static String trashBlocker(Collection<String> filePaths,
                                      Collection<String> folderPaths) {
        for (String p : filePaths) {
            if (p.indexOf('/') < 0 && lower(p).equals(lower(TRASH))) return p;
        }
        java.util.TreeSet<String> roots = new java.util.TreeSet<>();
        for (String f : FolderNav.allFolders(filePaths, folderPaths)) {
            int slash = f.indexOf('/');
            roots.add(slash >= 0 ? f.substring(0, slash) : f);
        }
        if (roots.contains(TRASH)) return null;
        for (String r : roots) {
            if (lower(r).equals(lower(TRASH))) return r;
        }
        return null;
    }

    /** Every folder on the server, keyed lower-cased, as it is spelt there. */
    public static Map<String, String> folderSpellings(Collection<String> filePaths,
                                                      Collection<String> folderPaths) {
        Map<String, String> out = new HashMap<>();
        for (String f : FolderNav.allFolders(filePaths, folderPaths)) out.put(lower(f), f);
        return out;
    }

    /**
     * {@code path} with each folder on the way spelt as the server spells it
     * ("docs/x" beside folder "Docs" becomes "Docs/x"): the server refuses a
     * second spelling of a folder. With {@code whole} the last segment is matched
     * too, so a folder that exists already is reused.
     */
    public static String matchCase(String path, Map<String, String> spelling, boolean whole) {
        String[] parts = path.split("/");
        int last = whole ? parts.length : parts.length - 1;
        String out = "";
        for (int i = 0; i < parts.length; i++) {
            String next = out.isEmpty() ? parts[i] : out + "/" + parts[i];
            String known = i < last ? spelling.get(lower(next)) : null;
            out = known != null ? known : next;
        }
        return out;
    }

    /**
     * Record the folders on the way to {@code path} (and {@code path} itself with
     * {@code whole}) in {@code spelling}, so later plans in a batch spell them alike.
     */
    static void learnSpelling(Map<String, String> spelling, String path, boolean whole) {
        String[] parts = path.split("/");
        int end = whole ? parts.length : parts.length - 1;
        String prefix = "";
        for (int i = 0; i < end; i++) {
            prefix = prefix.isEmpty() ? parts[i] : prefix + "/" + parts[i];
            if (!spelling.containsKey(lower(prefix))) spelling.put(lower(prefix), prefix);
        }
    }

    /** Every file at or below {@code folder} (recursive), sorted. */
    public static List<String> pathsUnder(String folder, Collection<String> filePaths) {
        String prefix = folder + "/";
        List<String> out = new ArrayList<>();
        for (String p : filePaths) {
            if (p.equals(folder) || p.startsWith(prefix)) out.add(p);
        }
        Collections.sort(out);
        return out;
    }

    // -- free names ("name (1).ext") ------------------------------------------------

    /** Tells whether a path is already in use. */
    public interface Taken {
        boolean test(String path);
    }

    /** {@code leaf} numbered Windows-style: "a.txt" → "a (1).txt"; a folder → "a (1)". */
    static String numbered(String leaf, int n, boolean folder) {
        int dot = leaf.lastIndexOf('.');
        if (folder || dot <= 0 || dot == leaf.length() - 1) return leaf + " (" + n + ")";
        return leaf.substring(0, dot) + " (" + n + ")" + leaf.substring(dot);
    }

    /** {@code path} if it's free, else the first free "name (n).ext" beside it. */
    public static String freePath(String path, Taken taken, boolean folder) {
        if (!taken.test(path)) return path;
        String parent = FolderNav.parent(path);
        String leaf = FolderNav.leaf(path);
        for (int n = 1; ; n++) {
            String name = numbered(leaf, n, folder);
            String candidate = parent.isEmpty() ? name : parent + "/" + name;
            if (!taken.test(candidate)) return candidate;
        }
    }

    /**
     * Every path in use on the server, live files and every folder, lower-cased.
     * The server refuses a name that differs from a taken one only by case (on a
     * case-insensitive disk it is the same file), so plans compare that way too.
     */
    public static Set<String> serverNames(Collection<String> filePaths,
                                          Collection<String> folderPaths) {
        Set<String> names = new HashSet<>();
        for (String p : filePaths) names.add(lower(p));
        for (String f : FolderNav.allFolders(filePaths, folderPaths)) names.add(lower(f));
        return names;
    }

    /**
     * {@code path} with any folder on the way that is really a file numbered like
     * a folder ("Trash/report/b.txt" beside file "Trash/report" becomes
     * "Trash/report (1)/b.txt"): the server refuses anything beneath a file.
     * Mirrors the desktop client's clear_ancestors.
     */
    static String clearAncestors(String path, Set<String> files, Set<String> taken) {
        String out = path;
        for (int slash = out.indexOf('/'); slash >= 0; slash = out.indexOf('/', slash + 1)) {
            String node = out.substring(0, slash);
            if (files.contains(lower(node))) {
                String free = freePath(node, q -> taken.contains(lower(q)), true);
                out = free + out.substring(slash);
                slash = free.length();
            }
        }
        return out;
    }

    // -- Trash and restore ------------------------------------------------------------

    /** Where each deleted item lands in Trash. */
    public static final class TrashPlan {
        /** {src, dst} file moves. */
        public final List<String[]> moves = new ArrayList<>();
        /** {folder, its place in Trash}, one per selected folder. */
        public final List<String[]> folderTargets = new ArrayList<>();
    }

    /**
     * Plan moving files and whole folders into Trash without ever landing on
     * something already there. A folder goes in whole: if Trash already holds
     * something with its name it becomes "name (1)", its contents kept together.
     * A file whose name is taken becomes "name (1).ext". Files inside a selected
     * folder travel with the folder. Folders on the way keep the spelling Trash
     * already has for them (see {@link #matchCase}).
     */
    public static TrashPlan planTrash(List<String> files, List<String> folders,
                                      Collection<String> filePaths,
                                      Collection<String> folderPaths) {
        Set<String> taken = serverNames(filePaths, folderPaths);
        Set<String> filesThere = new HashSet<>();
        for (String p : filePaths) filesThere.add(lower(p));
        Map<String, String> spelling = folderSpellings(filePaths, folderPaths);
        // Trash itself is never respelt onto a user's "trash" (see trashBlocker).
        spelling.put(lower(TRASH), TRASH);
        TrashPlan plan = new TrashPlan();
        for (String folder : folders) {
            String dst = freePath(
                    clearAncestors(matchCase(trashPath(folder), spelling, false), filesThere, taken),
                    p -> taken.contains(lower(p)), true);
            taken.add(lower(dst));
            learnSpelling(spelling, dst, true);
            plan.folderTargets.add(new String[]{folder, dst});
            for (String p : pathsUnder(folder, filePaths)) {
                String moved = dst + p.substring(folder.length());
                taken.add(lower(moved));
                plan.moves.add(new String[]{p, moved});
            }
        }
        for (String p : files) {
            if (underAny(p, folders)) continue;
            String dst = freePath(
                    clearAncestors(matchCase(trashPath(p), spelling, false), filesThere, taken),
                    q -> taken.contains(lower(q)), false);
            taken.add(lower(dst));
            learnSpelling(spelling, dst, false);
            plan.moves.add(new String[]{p, dst});
        }
        return plan;
    }

    /**
     * {explicit folder, its place in Trash} for every explicit folder at or below
     * each trashed folder (see {@link TrashPlan#folderTargets}). Files don't carry
     * these across, and an empty one has no files at all, so each is recreated in
     * Trash and its original removed; Restore brings them back the same way.
     */
    public static List<String[]> trashFolderMoves(List<String[]> folderTargets,
                                                  Collection<String> folderPaths) {
        List<String> sorted = new ArrayList<>(folderPaths);
        Collections.sort(sorted);
        List<String[]> out = new ArrayList<>();
        for (String[] t : folderTargets) {
            for (String f : sorted) {
                if (f.equals(t[0]) || f.startsWith(t[0] + "/")) {
                    out.add(new String[]{f, t[1] + f.substring(t[0].length())});
                }
            }
        }
        return out;
    }

    /**
     * Where trashed things came from, from each trashed file's moved_from
     * ({@code movedFrom}: path → the path it was last moved from). Trash numbers
     * a name that's already taken there ("a (1).txt"), so the Trash path alone
     * can't say where an item came from. The map holds each trashed file and
     * every trashed folder above it whose path lines up with the file's source.
     * Mirrors the desktop client's trash_origins.
     */
    public static Map<String, String> trashOrigins(Map<String, String> movedFrom) {
        Map<String, String> out = new java.util.HashMap<>();
        for (Map.Entry<String, String> e : movedFrom.entrySet()) {
            String path = e.getKey();
            String src = e.getValue();
            if (!inTrash(path) || src == null || src.isEmpty() || inTrash(src)) continue;
            String node = path;
            while (!node.equals(TRASH) && !node.isEmpty()) {
                String rest = path.substring(node.length());  // "" for the file itself
                if (src.length() <= rest.length() || !src.endsWith(rest)) break;
                if (!out.containsKey(node)) {
                    out.put(node, src.substring(0, src.length() - rest.length()));
                }
                node = FolderNav.parent(node);
            }
        }
        return out;
    }

    /** Where trashed {@code path} goes back to: from {@code origins} when known, else untrash. */
    public static String trashOrigin(String path, Map<String, String> origins) {
        String node = path;
        while (origins != null && !node.isEmpty() && !node.equals(TRASH)) {
            String o = origins.get(node);
            if (o != null) return o + path.substring(node.length());
            node = FolderNav.parent(node);
        }
        return untrash(path);
    }

    /**
     * {trashed folder entry, where it goes back} for each explicit folder inside
     * Trash. A folder that exists there already, in any case, is simply reused;
     * {@code filePaths} should include where the batch's files are going, so a
     * folder is spelt as they are.
     */
    public static List<String[]> restoreFolderMoves(List<String> folderEntries,
                                                    Map<String, String> origins,
                                                    Collection<String> filePaths,
                                                    Collection<String> folderPaths) {
        Map<String, String> spelling = folderSpellings(filePaths, folderPaths);
        Set<String> files = new HashSet<>();
        for (String p : filePaths) files.add(lower(p));
        List<String[]> out = new ArrayList<>();
        for (String f : folderEntries) {
            if (!f.startsWith(TRASH + "/")) continue;
            String dst = matchCase(trashOrigin(f, origins), spelling, true);
            // Numbered as its files are (see planRestore) if the name is now a file.
            String checked = clearAncestors(dst + "/-", files, files);
            dst = checked.substring(0, checked.length() - 2);
            learnSpelling(spelling, dst, true);
            out.add(new String[]{f, dst});
        }
        return out;
    }

    /**
     * {src, dst} moves that put trashed files back where they came from (see
     * {@link #trashOrigins}). One whose spot has been taken since comes back as
     * "name (1).ext" beside it, and a folder on the way that exists in another
     * case is used as it is spelt.
     */
    public static List<String[]> planRestore(List<String> paths, Collection<String> filePaths,
                                             Collection<String> folderPaths,
                                             Map<String, String> origins) {
        Set<String> taken = serverNames(filePaths, folderPaths);
        Set<String> filesThere = new HashSet<>();
        for (String p : filePaths) filesThere.add(lower(p));
        Map<String, String> spelling = folderSpellings(filePaths, folderPaths);
        List<String[]> moves = new ArrayList<>();
        for (String p : paths) {
            String dst = freePath(
                    clearAncestors(matchCase(trashOrigin(p, origins), spelling, false), filesThere, taken),
                    q -> taken.contains(lower(q)), false);
            taken.add(lower(dst));
            learnSpelling(spelling, dst, false);
            moves.add(new String[]{p, dst});
        }
        return moves;
    }

    // -- uploads ----------------------------------------------------------------------

    /**
     * The picked names that can't be uploaded into {@code cwd}: uploads never
     * replace anything, so a name already used there on the server — by a file or
     * a folder — is refused, as is a second picked file with the same name.
     * Returns the index of each refused name.
     */
    public static List<Integer> uploadClashes(List<String> names, String cwd,
                                              Collection<String> filePaths,
                                              Collection<String> folderPaths) {
        Set<String> taken = serverNames(filePaths, folderPaths);  // lower-cased
        taken.add(lower(TRASH));  // reserved for deleted items, even before anything is deleted
        Set<String> seen = new HashSet<>();
        List<Integer> clashes = new ArrayList<>();
        for (int i = 0; i < names.size(); i++) {
            String server;
            try {
                server = normalize(cwd.isEmpty() ? names.get(i) : cwd + "/" + names.get(i));
            } catch (IllegalArgumentException e) {
                clashes.add(i);
                continue;
            }
            String key = lower(server);
            if (taken.contains(key) || !seen.add(key)) clashes.add(i);
        }
        return clashes;
    }

    // -- downloads --------------------------------------------------------------------

    /** Answers whether something already exists at a path inside the download folder. */
    public interface LocalProbe {
        boolean exists(String rel);
    }

    /** One file to download: its server path and where it's written. */
    public static final class Download {
        public final String path;
        public final long size;
        public String dest;          // relative to the download folder
        public boolean overwrite;    // the user chose to replace a file already there

        Download(String path, long size, String dest) {
            this.path = path;
            this.size = size;
            this.dest = dest;
        }
    }

    public static final class DownloadPlan {
        public final List<Download> items = new ArrayList<>();
        /** Folders created up front, so empty sub-folders come along too. */
        public final List<String> dirs = new ArrayList<>();
        /** Selected folders whose name is already taken in the download folder. */
        public final List<String> folderClashes = new ArrayList<>();
        /** Files whose name is already taken in the download folder. */
        public final List<Download> fileClashes = new ArrayList<>();
    }

    /**
     * Work out where each download goes, and which names are already taken.
     *
     * A file lands straight in the download folder; a folder lands as
     * "&lt;its name&gt;/…" with everything below it. A folder whose name is taken is
     * refused outright; a file whose name is taken is left in fileClashes for the
     * user to decide. Two items of one batch that would share a name aren't a
     * clash — the later one is simply saved as "name (1).ext".
     */
    public static DownloadPlan planDownloads(List<String> files, List<String> folders,
                                             Map<String, Long> sizes,
                                             Collection<String> folderPaths,
                                             LocalProbe probe) {
        DownloadPlan plan = new DownloadPlan();
        Set<String> taken = new HashSet<>();  // lower-cased: phone storage ignores case
        for (String folder : folders) {
            String name = normalize(FolderNav.leaf(folder));
            if (taken.contains(lower(name)) || probe.exists(name)) {
                plan.folderClashes.add(folder);
                continue;
            }
            taken.add(lower(name));
            plan.dirs.add(name);
            String prefix = folder + "/";
            List<String> subs = new ArrayList<>(folderPaths);
            Collections.sort(subs);
            for (String sub : subs) {
                if (sub.startsWith(prefix)) {
                    plan.dirs.add(normalize(name + "/" + sub.substring(prefix.length())));
                }
            }
            for (String p : pathsUnder(folder, sizes.keySet())) {
                String dest = normalize(name + "/" + p.substring(prefix.length()));
                plan.items.add(new Download(p, sizeOf(sizes, p), dest));
            }
        }
        for (String p : files) {
            if (underAny(p, folders)) continue;  // already coming with its folder
            String dest = normalize(FolderNav.leaf(p));
            if (taken.contains(lower(dest))) {
                dest = freePath(dest, rel -> taken.contains(lower(rel)) || probe.exists(rel), false);
            } else if (probe.exists(dest)) {
                plan.fileClashes.add(new Download(p, sizeOf(sizes, p), dest));
                continue;
            }
            taken.add(lower(dest));
            plan.items.add(new Download(p, sizeOf(sizes, p), dest));
        }
        return plan;
    }

    /** Apply the user's answer — OVERWRITE, KEEP_BOTH or SKIP — to every clash. */
    public static void resolveClashes(DownloadPlan plan, String choice, LocalProbe probe) {
        List<Download> clashes = new ArrayList<>(plan.fileClashes);
        plan.fileClashes.clear();
        if (SKIP.equals(choice)) return;
        Set<String> taken = new HashSet<>();
        for (Download d : plan.items) taken.add(lower(d.dest));
        for (String dir : plan.dirs) taken.add(lower(dir));
        for (Download d : clashes) {
            // Two clashing files with one name can't both overwrite it: the second
            // keeps both instead.
            if (OVERWRITE.equals(choice) && !taken.contains(lower(d.dest))) {
                d.overwrite = true;
            } else {
                d.dest = freePath(d.dest,
                        rel -> taken.contains(lower(rel)) || probe.exists(rel), false);
            }
            taken.add(lower(d.dest));
            plan.items.add(d);
        }
    }

    private static boolean underAny(String path, List<String> folders) {
        for (String f : folders) {
            if (path.startsWith(f + "/")) return true;
        }
        return false;
    }

    private static long sizeOf(Map<String, Long> sizes, String path) {
        Long s = sizes.get(path);
        return s == null ? 0 : s;
    }

    /**
     * A name's case-insensitive identity, as close to the server's casefold() as
     * Java gets: through upper case first, so "ß" folds to "ss" as it does there.
     */
    private static String lower(String s) {
        return s.toUpperCase(Locale.ROOT).toLowerCase(Locale.ROOT);
    }
}
