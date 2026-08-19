using ETuner.Models;

namespace ETuner.Services;

public class CategoryAdjectiveNounStrategy : INamingStrategy
{
    public string DisplayName => "Adjective-Noun";
    public string Description => "Category + random adjective + noun, e.g. Audio_Creative_Mountain.mp3";

    private static readonly string[] Adjectives = {
        "Creative", "Brave", "Silent", "Wild", "Mellow", "Bold", "Gentle", "Fierce",
        "Calm", "Sharp", "Soft", "Bright", "Quick", "Steady", "Loyal", "Free",
        "Eager", "Happy", "Clever", "Swift"
    };

    private static readonly string[] Nouns = {
        "Mountain", "River", "Phoenix", "Storm", "Shadow", "Echo", "Flame", "Stone",
        "Wind", "Star", "Wolf", "Falcon", "Oak", "Ocean", "Thunder", "Lion",
        "Eagle", "Fire", "Forest", "Comet"
    };

    private readonly Random _rng = new();

    public string GenerateName(RenamePreview preview, FileCategory category, int index)
    {
        var ext = System.IO.Path.GetExtension(preview.OriginalName) ?? "";
        var adj = Adjectives[_rng.Next(Adjectives.Length)];
        var noun = Nouns[_rng.Next(Nouns.Length)];
        return $"{category}_{adj}_{noun}{ext}";
    }
}
