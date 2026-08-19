using System.IO;
using ETuner.Models;

namespace ETuner.Services;

public class FileClassifier
{
    private static readonly Dictionary<string, FileCategory> AudioExts = new()
    {
        { ".mp3", FileCategory.Audio },
        { ".flac", FileCategory.Audio },
        { ".ogg", FileCategory.Audio },
        { ".oga", FileCategory.Audio },
        { ".opus", FileCategory.Audio },
        { ".m4a", FileCategory.Audio },
        { ".mp4", FileCategory.Audio },
        { ".wav", FileCategory.Audio },
        { ".wma", FileCategory.Audio },
        { ".aac", FileCategory.Audio },
        { ".m4b", FileCategory.Audio },
    };

    private static readonly Dictionary<string, FileCategory> VideoExts = new()
    {
        { ".mkv", FileCategory.Video },
        { ".avi", FileCategory.Video },
        { ".mov", FileCategory.Video },
        { ".wmv", FileCategory.Video },
        { ".flv", FileCategory.Video },
        { ".webm", FileCategory.Video },
        { ".mpg", FileCategory.Video },
        { ".mpeg", FileCategory.Video },
    };

    private static readonly Dictionary<string, FileCategory> DocumentExts = new()
    {
        { ".txt", FileCategory.Document },
        { ".pdf", FileCategory.Document },
        { ".doc", FileCategory.Document },
        { ".docx", FileCategory.Document },
        { ".xls", FileCategory.Document },
        { ".xlsx", FileCategory.Document },
        { ".ppt", FileCategory.Document },
        { ".pptx", FileCategory.Document },
        { ".rtf", FileCategory.Document },
        { ".md", FileCategory.Document },
        { ".csv", FileCategory.Document },
    };

    private static readonly Dictionary<string, FileCategory> ImageExts = new()
    {
        { ".jpg", FileCategory.Image },
        { ".jpeg", FileCategory.Image },
        { ".png", FileCategory.Image },
        { ".gif", FileCategory.Image },
        { ".bmp", FileCategory.Image },
        { ".tiff", FileCategory.Image },
        { ".tif", FileCategory.Image },
        { ".webp", FileCategory.Image },
        { ".svg", FileCategory.Image },
        { ".ico", FileCategory.Image },
    };

    private static readonly Dictionary<string, FileCategory> ArchiveExts = new()
    {
        { ".zip", FileCategory.Archive },
        { ".rar", FileCategory.Archive },
        { ".7z", FileCategory.Archive },
        { ".tar", FileCategory.Archive },
        { ".gz", FileCategory.Archive },
        { ".bz2", FileCategory.Archive },
    };

    public FileCategory DetectCategory(FileInfo file)
    {
        var ext = file.Extension.ToLowerInvariant();

        if (ext == ".mp4")
        {
            var tags = MetadataReader.ReadTags(file.FullName);
            if (tags != null && !string.IsNullOrEmpty(tags.Title))
                return FileCategory.Audio;
            return FileCategory.Video;
        }

        if (AudioExts.TryGetValue(ext, out var cat)) return cat;
        if (VideoExts.TryGetValue(ext, out var cat2)) return cat2;
        if (DocumentExts.TryGetValue(ext, out var cat3)) return cat3;
        if (ImageExts.TryGetValue(ext, out var cat4)) return cat4;
        if (ArchiveExts.TryGetValue(ext, out var cat5)) return cat5;

        return FileCategory.Other;
    }

    public string DeterminePurpose(FileClassification file, TagInfo? tags)
    {
        if (file.Category == FileCategory.Audio && tags != null)
        {
            var issues = new List<string>();
            if (tags.HasDuplicateArtists()) issues.Add("dup artist names");
            if (tags.NeedsAlbumArtist()) issues.Add("missing album_artist");
            if (tags.NeedsSortField()) issues.Add("missing album_artist_sort");
            if (!string.IsNullOrWhiteSpace(tags.Artist) && tags.Artist.Contains("feat.", StringComparison.OrdinalIgnoreCase))
                issues.Add("feat in artist");

            if (issues.Any())
                return string.Join(", ", issues);
            return "clean";
        }

        return file.Category.ToString().ToLower();
    }

    public List<FileClassification> ClassifyFolder(DirectoryInfo folder, bool recursive = true, bool includeHidden = false)
    {
        var results = new List<FileClassification>();

        if (!folder.Exists) return results;

        var searchOption = recursive ? SearchOption.AllDirectories : SearchOption.TopDirectoryOnly;
        var files = folder.GetFiles("*", searchOption);

        foreach (var file in files)
        {
            if ((file.Attributes & FileAttributes.Hidden) != 0 && !includeHidden)
                continue;

            var category = DetectCategory(file);
            var tags = category == FileCategory.Audio
                ? MetadataReader.ReadTags(file.FullName)
                : null;

            var classification = new FileClassification
            {
                Name = file.Name,
                Extension = file.Extension,
                FullPath = file.FullName,
                Size = file.Length,
                Category = category,
                IsSelected = true,
                Status = "Pending"
            };

            classification.Purpose = DeterminePurpose(classification, tags);
            results.Add(classification);
        }

        return results;
    }
}
