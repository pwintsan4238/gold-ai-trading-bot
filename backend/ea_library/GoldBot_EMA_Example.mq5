//+------------------------------------------------------------------+
//| GoldBot_EMA_Example.mq5                                          |
//| Sample Expert Advisor shipped with the Gold AI Trading Bot.      |
//| EMA20/EMA50 crossover, ATR stop, fixed % risk, daily limits.     |
//| Educational — run it on a DEMO account.                          |
//+------------------------------------------------------------------+
#property copyright "Gold AI Trading Bot"
#property version   "1.00"
#include <Trade\Trade.mqh>

input double RiskPercent     = 1.0;     // Risk per trade (% of balance)
input int    FastEMA         = 20;      // Fast EMA period
input int    SlowEMA         = 50;      // Slow EMA period
input int    ATRPeriod       = 14;      // ATR period
input double SL_ATR_Mult     = 1.5;     // Stop-loss = ATR x this
input double RewardRisk      = 1.5;     // Take-profit = stop distance x this
input int    MaxTradesPerDay = 5;       // Max new trades per day
input double MaxDailyLossPct = 3.0;     // Stop for the day after this % loss
input ulong  MagicNumber     = 770078;  // Identifies this EA's trades

CTrade   trade;
int      hFast, hSlow, hATR;
datetime lastBar = 0;
double   dayStartBalance = 0;
int      today = -1;

int OnInit()
  {
   hFast = iMA(_Symbol, _Period, FastEMA, 0, MODE_EMA, PRICE_CLOSE);
   hSlow = iMA(_Symbol, _Period, SlowEMA, 0, MODE_EMA, PRICE_CLOSE);
   hATR  = iATR(_Symbol, _Period, ATRPeriod);
   if(hFast == INVALID_HANDLE || hSlow == INVALID_HANDLE || hATR == INVALID_HANDLE)
      return(INIT_FAILED);
   trade.SetExpertMagicNumber(MagicNumber);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   IndicatorRelease(hFast);
   IndicatorRelease(hSlow);
   IndicatorRelease(hATR);
  }

bool HasPosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(PositionSelectByTicket(t) &&
         PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == MagicNumber)
         return(true);
     }
   return(false);
  }

int TradesToday()
  {
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   dt.hour = 0; dt.min = 0; dt.sec = 0;
   if(!HistorySelect(StructToTime(dt), TimeCurrent()))
      return(0);
   int n = 0;
   for(int i = HistoryDealsTotal() - 1; i >= 0; i--)
     {
      ulong d = HistoryDealGetTicket(i);
      if((ulong)HistoryDealGetInteger(d, DEAL_MAGIC) == MagicNumber &&
         HistoryDealGetInteger(d, DEAL_ENTRY) == DEAL_ENTRY_IN &&
         HistoryDealGetString(d, DEAL_SYMBOL) == _Symbol)
         n++;
     }
   return(n);
  }

double LotsForRisk(double slDist)
  {
   double riskMoney = AccountInfoDouble(ACCOUNT_BALANCE) * RiskPercent / 100.0;
   double tickVal   = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickVal <= 0 || tickSize <= 0 || slDist <= 0)
      return(0);
   double lossPerLot = slDist / tickSize * tickVal;
   double lots = riskMoney / lossPerLot;
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double minL = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxL = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   lots = MathFloor(lots / step) * step;
   if(lots < minL)
      return(0);            // account too small for this stop: skip, never over-risk
   return(MathMin(lots, maxL));
  }

void OnTick()
  {
   datetime bar = iTime(_Symbol, _Period, 0);
   if(bar == lastBar)
      return;               // act once per new candle
   lastBar = bar;

   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if(dt.day_of_year != today)
     {
      today = dt.day_of_year;
      dayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
     }
   if(dayStartBalance > 0 &&
      (dayStartBalance - AccountInfoDouble(ACCOUNT_EQUITY)) / dayStartBalance * 100.0 >= MaxDailyLossPct)
      return;               // daily loss limit reached
   if(HasPosition() || TradesToday() >= MaxTradesPerDay)
      return;

   double f[2], s[2], a[1];
   if(CopyBuffer(hFast, 0, 1, 2, f) < 2 || CopyBuffer(hSlow, 0, 1, 2, s) < 2 ||
      CopyBuffer(hATR, 0, 1, 1, a) < 1)
      return;
   // f[0] = two bars ago, f[1] = last closed bar
   bool crossUp   = f[0] <= s[0] && f[1] > s[1];
   bool crossDown = f[0] >= s[0] && f[1] < s[1];
   if(!crossUp && !crossDown)
      return;

   double slDist = a[0] * SL_ATR_Mult;
   double lots   = LotsForRisk(slDist);
   if(lots <= 0)
      return;
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);

   if(crossUp)
     {
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      trade.Buy(lots, _Symbol, ask, NormalizeDouble(ask - slDist, digits),
                NormalizeDouble(ask + slDist * RewardRisk, digits), "GoldBot EA");
     }
   else
     {
      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      trade.Sell(lots, _Symbol, bid, NormalizeDouble(bid + slDist, digits),
                 NormalizeDouble(bid - slDist * RewardRisk, digits), "GoldBot EA");
     }
  }
//+------------------------------------------------------------------+
