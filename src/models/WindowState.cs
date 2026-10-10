using System.ComponentModel;
using System.Runtime.CompilerServices;
using LiveCaptionsTranslator.Utils;

namespace LiveCaptionsTranslator.models
{
    public class MainWindowState : INotifyPropertyChanged
    {
        public event PropertyChangedEventHandler? PropertyChanged;

        private bool topmost = true;
        private bool captionLogEnabled = false;
        private bool latencyShow = false;

        // Main caption appearance. Values are persisted in setting.json.
        // Older settings files automatically use these defaults.
        public string OriginalFontFamily { get; set; } = "Yu Gothic UI";
        public int OriginalFontSize { get; set; } = 20;
        public string OriginalFontColor { get; set; } = "#F5F5F5";
        public string TranslatedFontFamily { get; set; } = "Microsoft YaHei UI";
        public int TranslatedFontSize { get; set; } = 24;
        public string TranslatedFontColor { get; set; } = "#8DDBFF";
        public string CaptionBackgroundColor { get; set; } = "#1B1B1B";

        public bool Topmost
        {
            get => topmost;
            set
            {
                topmost = value;
                OnPropertyChanged("Topmost");
            }
        }
        public bool CaptionLogEnabled
        {
            get => captionLogEnabled;
            set
            {
                captionLogEnabled = value;
                OnPropertyChanged("CaptionLogEnabled");
            }
        }
        public bool LatencyShow
        {
            get => latencyShow;
            set
            {
                latencyShow = value;
                OnPropertyChanged("LatencyShow");
            }
        }

        public void OnPropertyChanged([CallerMemberName] string propName = "")
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propName));
            Translator.Setting?.Save();
        }
    }

    public class OverlayWindowState : INotifyPropertyChanged
    {
        public event PropertyChangedEventHandler? PropertyChanged;

        private int fontSize = 15;
        private Color fontColor = Color.White;
        private FontBold fontBold = FontBold.None;
        private double fontStroke = 0.0;
        
        private Color backgroundColor = Color.Black;
        private int opacity = 150;

        public int FontSize
        {
            get => fontSize;
            set
            {
                fontSize = value;
                OnPropertyChanged("FontSize");
            }
        }
        public Color FontColor
        {
            get => fontColor;
            set
            {
                fontColor = value;
                OnPropertyChanged("FontColor");
            }
        }
        public FontBold FontBold
        {
            get => fontBold;
            set
            {
                fontBold = value;
                OnPropertyChanged("FontBold");
            }
        }
        public double FontStroke
        {
            get => fontStroke;
            set
            {
                fontStroke = value;
                OnPropertyChanged("FontStroke");
            }
        }
        public Color BackgroundColor
        {
            get => backgroundColor;
            set
            {
                backgroundColor = value;
                OnPropertyChanged("BackgroundColor");
            }
        }
        public int Opacity
        {
            get => opacity;
            set
            {
                opacity = value;
                OnPropertyChanged("Opacity");
            }
        }

        public void OnPropertyChanged([CallerMemberName] string propName = "")
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propName));
            Translator.Setting?.Save();
        }
    }
}