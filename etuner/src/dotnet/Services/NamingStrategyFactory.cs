using ETuner.Models;

namespace ETuner.Services;

public static class NamingStrategyFactory
{
    public static readonly List<INamingStrategy> AllStrategies = new()
    {
        new CategoryNumberStrategy(),
        new CategoryHashStrategy(),
        new CategoryTimestampStrategy(),
        new CategoryAdjectiveNounStrategy(),
    };

    public static INamingStrategy Create(string name)
    {
        return AllStrategies.FirstOrDefault(s => s.DisplayName == name)
               ?? AllStrategies[0];
    }
}
