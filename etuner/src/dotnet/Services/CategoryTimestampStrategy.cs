using ETuner.Models;

namespace ETuner.Services;

public class CategoryTimestampStrategy : INamingStrategy
{
    public string DisplayName => "Category Timestamp";
    public string Description => "Category prefix + timestamp, e.g. Audio_20240115_142530.mp3";

    public string GenerateName(RenamePreview preview, FileCategory category, int index)
    {
        var ext = System.IO.Path.GetExtension(preview.OriginalName) ?? "";
        var ts = DateTime.Now;
        return $"{category}_{ts:yyyyMMdd_HHmmss}_{index + 1:000}{ext}";
    }
}
