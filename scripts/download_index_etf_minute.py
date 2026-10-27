"""Acquire the eight arbitrage indices/ETFs using pinned daily coverage."""
from scripts.download_index_futures_minute import main


if __name__ == '__main__':
    main(dataset='index_etf')
