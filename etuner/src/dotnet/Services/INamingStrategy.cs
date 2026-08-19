using ETuner.Models;

namespace ETuner.Services
{
    public interface INamingStrategy
    {
        string GenerateName(RenamePreview preview, FileCategory category, int index);
        string DisplayName { get; }
        string Description { get; }
    }
}
