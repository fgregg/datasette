.. _csv_export:

CSV export
==========

Any Datasette table, view or custom SQL query can be exported as CSV.

To obtain the CSV representation of the table you are looking, click the "this
data as CSV" link.

You can also use the advanced export form for more control over the resulting
file, which looks like this and has the following options:

.. image:: https://github.com/simonw/datasette-screenshots/blob/0.62/advanced-export.png?raw=true
   :alt: Advanced export form. You can get the data in different JSON shapes, and CSV options are download file, expand labels and stream all rows.

* **download file** - instead of displaying CSV in your browser, this forces
  your browser to download the CSV to your downloads directory.

* **expand labels** - if your table has any foreign key references this option
  will cause the CSV to gain additional ``COLUMN_NAME_label`` columns with a
  label for each foreign key derived from the linked table. `In this example
  <https://latest.datasette.io/fixtures/facetable.csv?_labels=on&_size=max>`_
  the ``city_id`` column is accompanied by a ``city_id_label`` column.

* **stream all rows** - by default CSV files only contain the first
  :ref:`setting_max_returned_rows` records. This option will cause Datasette to
  loop through every matching record and return them as a single CSV file.

You can try that out on https://latest.datasette.io/fixtures/facetable?_size=4

.. _csv_export_url_parameters:

URL parameters
--------------

The following options can be used to customize the CSVs returned by Datasette.

``?_header=off``
    This removes the first row of the CSV file specifying the headings - only the row data will be returned.

``?_stream=on``
    Stream all matching records, not just the first page of results. See below.

``?_dl=on``
    Causes Datasette to return a ``content-disposition: attachment; filename="filename.csv"`` header.

Streaming all records
---------------------

The *stream all rows* option streams the full result set from a single
server-side cursor over a dedicated connection, fetching rows in chunks rather
than paginating through the table. This works for tables, views and arbitrary
SQL queries alike, and is not capped at :ref:`setting_max_returned_rows`.

The number of rows fetched per chunk is controlled by the
:ref:`setting_max_csv_stream_page_size` setting (default 10000). Larger chunks
mean fewer round-trips at the cost of higher peak memory per chunk.

Streaming a query that hangs in the database engine (for example a blocking
sort before the first row) is bounded per chunk by :ref:`setting_sql_time_limit_ms`.
A download that is abandoned by the client releases its connection immediately.
The :ref:`setting_max_csv_mb` cap does **not** apply to streamed exports — a
stream is intended to return every matching row. You can disable the CSV export
feature entirely using :ref:`setting_allow_csv_stream`.
