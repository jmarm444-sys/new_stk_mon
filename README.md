courtesy of gemini and grok- 
this is a modification to a previous app called galert_mon.py
Here is how the data flow works without a database:
The Static Storage (Your Master File)
You keep a single CSV file on your computer containing the fixed details: Symbol, Account Name, Shares, and Original Cost. This file only changes when you manually update it after buying or selling a stock.
The In-Memory Processing
When your Python script runs (perhaps triggered automatically by Windows Task Scheduler after market close), it reads that master CSV file into memory. It then reaches out to yfinance to download the current price and the 30-day historical price for all symbols at once. These live prices are loaded into a second in-memory DataFrame.
The Comparison and Export
Python merges these two memory tables together, instantly calculating the daily and monthly percentage changes, it also displays total_change% and total_change$, It applies your filters to drop the quiet stocks, leaving only the volatile movers. Finally, it exports this filtered memory table straight to a new daily alert CSV file. Once the script finishes, the memory is cleared.
