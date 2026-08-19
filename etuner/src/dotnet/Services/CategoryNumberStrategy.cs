using ETuner.Models;

namespace ETuner.Services;

public class CategoryNumberStrategy : INamingStrategy
{
    public string DisplayName => "Category Number";
    public string Description => "Prefix by category, e.g. Audio_001.mp3";

    public string GenerateName(RenamePreview preview, FileCategory category, int index)
    {
        var ext = System.IO.Path.GetExtension(preview.OriginalName) ?? "";
        return $"{category}_{index + 1:000}{ext}";
    }
}
