using System.ComponentModel;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;

using LiveCaptionsTranslator.utils;

namespace LiveCaptionsTranslator
{
    public partial class CaptionPage : Page
    {
        public const int CARD_HEIGHT = 110;

        private static CaptionPage instance;
        public static CaptionPage Instance => instance;

        public CaptionPage()
        {
            InitializeComponent();
            DataContext = Translator.Caption;
            instance = this;

            ApplyCaptionAppearance();

            Loaded += (s, e) =>
            {
                AutoHeight();
                (App.Current.MainWindow as MainWindow).CaptionLogButton.Visibility = Visibility.Visible;
                Translator.Caption.PropertyChanged += TranslatedChanged;
            };
            Unloaded += (s, e) =>
            {
                (App.Current.MainWindow as MainWindow).CaptionLogButton.Visibility = Visibility.Collapsed;
                Translator.Caption.PropertyChanged -= TranslatedChanged;
            };

            CollapseTranslatedCaption(Translator.Setting.MainWindow.CaptionLogEnabled);
        }

        private async void TextBlock_MouseLeftButtonDown(object sender, RoutedEventArgs e)
        {
            if (sender is TextBlock textBlock)
            {
                try
                {
                    Clipboard.SetText(textBlock.Text);
                    SnackbarHost.Show("Copied.", textBlock.Text, SnackbarType.Info, 100);
                }
                catch
                {
                    SnackbarHost.Show("Copy Failed.", string.Empty, SnackbarType.Error, 100);
                }
                await Task.Delay(500);
            }
        }

        private void ApplyCaptionAppearance()
        {
            var appearance = Translator.Setting.MainWindow;
            OriginalCaption.FontFamily = new FontFamily(
                string.IsNullOrWhiteSpace(appearance.OriginalFontFamily) ? "Yu Gothic UI" : appearance.OriginalFontFamily);
            TranslatedCaption.FontFamily = new FontFamily(
                string.IsNullOrWhiteSpace(appearance.TranslatedFontFamily) ? "Microsoft YaHei UI" : appearance.TranslatedFontFamily);
            OriginalCaption.FontSize = Math.Clamp(appearance.OriginalFontSize, 8, 40);
            OriginalCaption.Foreground = ParseColor(appearance.OriginalFontColor, Brushes.White);
            TranslatedCaption.Foreground = ParseColor(appearance.TranslatedFontColor, Brushes.LightBlue);
            var background = ParseColor(appearance.CaptionBackgroundColor, Brushes.Black);
            OriginalCaptionCard.Background = background;
            TranslatedCaptionCard.Background = background;
            ApplyTranslatedFontSize();
        }

        private static Brush ParseColor(string? value, Brush fallback)
        {
            if (string.IsNullOrWhiteSpace(value)) return fallback;
            try
            {
                return new BrushConverter().ConvertFromString(value) as Brush ?? fallback;
            }
            catch (FormatException)
            {
                return fallback;
            }
            catch (NotSupportedException)
            {
                return fallback;
            }
        }

        private void ApplyTranslatedFontSize()
        {
            int preferredSize = Math.Clamp(Translator.Setting.MainWindow.TranslatedFontSize, 8, 40);
            // Preserve the original 18 -> 15 long-text adjustment proportionally.
            bool isLong = Encoding.UTF8.GetByteCount(Translator.Caption.DisplayTranslatedCaption ?? string.Empty)
                          >= TextUtil.LONG_THRESHOLD;
            TranslatedCaption.FontSize = isLong
                ? Math.Max(8, Math.Round(preferredSize * 15.0 / 18.0))
                : preferredSize;
        }

        private void TranslatedChanged(object sender, PropertyChangedEventArgs e)
        {
            if (e.PropertyName == nameof(Translator.Caption.DisplayTranslatedCaption))
                Dispatcher.BeginInvoke(new Action(ApplyTranslatedFontSize), DispatcherPriority.Background);
        }

        public void CollapseTranslatedCaption(bool isCollapsed)
        {
            var converter = new GridLengthConverter();

            if (isCollapsed)
            {
                TranslatedCaption_Row.Height = (GridLength)converter.ConvertFromString("Auto");
                LogCards.Visibility = Visibility.Visible;
            }
            else
            {
                TranslatedCaption_Row.Height = (GridLength)converter.ConvertFromString("*");
                LogCards.Visibility = Visibility.Collapsed;
            }
        }

        public void AutoHeight()
        {
            if (Translator.Setting.MainWindow.CaptionLogEnabled)
                (App.Current.MainWindow as MainWindow).AutoHeightAdjust(
                    minHeight: CARD_HEIGHT * (Translator.Setting.DisplaySentences + 1),
                    maxHeight: CARD_HEIGHT * (Translator.Setting.DisplaySentences + 1));
            else
                (App.Current.MainWindow as MainWindow).AutoHeightAdjust(
                    minHeight: (int)App.Current.MainWindow.MinHeight,
                    maxHeight: (int)App.Current.MainWindow.MinHeight);
        }
    }
}
