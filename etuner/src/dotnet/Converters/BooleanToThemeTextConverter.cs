namespace ETuner.Converters;

using System;
using System.Globalization;
using System.Windows.Data;

public class BooleanToThemeTextConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
    {
        return (value as bool?) == true ? "☀" : "☾";
    }

    public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
    {
        throw new NotImplementedException();
    }
}
