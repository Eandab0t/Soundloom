using System.Security.Cryptography;
using System.Text;
using ETuner.Models;

namespace ETuner.Services;

public class CategoryHashStrategy : INamingStrategy
{
    public string DisplayName => "Category Hash";
    public string Description => "Category prefix + content hash, e.g. Audio_a3b8f2.mp3";

    public string GenerateName(RenamePreview preview, FileCategory category, int index)
    {
        var ext = System.IO.Path.GetExtension(preview.OriginalName) ?? "";
        var hash = ComputeShortHash(preview.OriginalName);
        return $"{category}_{hash}{ext}";
    }

    private static string ComputeShortHash(string input)
    {
        using var sha = SHA256.Create();
        var bytes = sha.ComputeHash(Encoding.UTF8.GetBytes(input));
        return BitConverter.ToString(bytes, 0, 3).Replace("-", "").ToLowerInvariant();
    }
}
